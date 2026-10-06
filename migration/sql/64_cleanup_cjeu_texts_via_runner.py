#!/usr/bin/env python3
"""Apply the CJEU case_text classification and a data-preserving cleanup.

Input is the classification produced by 63 (CLASSIFICATION_CSV: id, ecli,
language, source, kind, celex, rule, head). Steps, each idempotent:

1. archive   every row that step 4/5 deletes or moves (full row) and every
             case whose CELEX step 6 changes, as gzipped JSON lines.
2. classify  write document_kind/document_celex where still NULL.
3. summaries summaries are copied onto every text row of a (case, language);
             before a row leaves its slot, copy its summary onto a remaining
             row of that slot, or keep a summary-only row there.
4. drop      misfiled texts identical to a text already stored under the
             case they belong to (no information is lost).
5. move      misfiled texts whose rightful case is identified (same case
             number, its decision date in the text's opening, or its corpus
             text) and has no text from that source in that language.
6. celex     cases stored under a derived CELEX (62015CJ0005_SUM) take the
             CELEX of the decision itself when no other case holds it.
7. link      unresolved CELLAR citations whose target CELEX now resolves.

Misfiled texts whose case cannot be identified stay, labelled 'misfiled'
(never served by case_text_canonical or the API).

Env:
  SQL_RUNNER_URL, SQL_RUNNER_TOKEN, SQL_RUNNER_CONFIRM (default execute-cle-v2)
  CLASSIFICATION_CSV  output of 63 (REPORT_TSV of a dry run)
  ARCHIVE_DIR         where step 1 writes its archive (required to write)
  DRY_RUN             default 1: print the plan, write nothing
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

DECISION_KINDS = {"judgment", "order", "opinion", "ruling", "decision", "other"}
DERIVED_SUFFIX = re.compile(r"_(SUM|RES|INF|EXT)$")
CHUNK = 1_000


def runner(sql, params=None, execute=False):
    url = os.environ["SQL_RUNNER_URL"].rstrip("/") + ("/execute" if execute else "/query")
    payload = {"sql": sql}
    if params is not None:
        payload["params"] = params
    if execute:
        payload["confirm"] = os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2")
    body = json.dumps(payload, separators=(",", ":")).encode()
    last = None
    for attempt in range(5):
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        msg = "\n".join(
            ["POST", urllib.parse.urlparse(url).path, ts, nonce, hashlib.sha256(body).hexdigest()]
        ).encode()
        sig = hmac.new(os.environ["SQL_RUNNER_TOKEN"].encode(), msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json", "X-SQL-Runner-Timestamp": ts,
            "X-SQL-Runner-Nonce": nonce, "X-SQL-Runner-Signature": f"v1={sig}"})
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                out = json.loads(response.read())
            if not out.get("ok"):
                raise RuntimeError(str(out)[:300])
            return out
        except urllib.error.HTTPError as exc:
            last = RuntimeError(f"HTTP {exc.code}: {exc.read().decode()[:300]}")
            if "QueryCanceled" in str(last):
                raise last
        except Exception as exc:  # noqa: BLE001 - transport errors are retried
            last = exc
        time.sleep(3 * (attempt + 1))
    raise last


def rows(sql, params=None):
    return runner(sql, params)["rows"]


def chunked(items, size=CHUNK):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def execute_chunks(label, sql, items, columns):
    """Run `sql` over `items` (tuples) in chunks; halve chunks on timeouts."""
    done = 0

    def run(batch):
        nonlocal done
        try:
            out = runner(sql, [[item[i] for item in batch] for i in range(columns)], execute=True)
            done += out.get("row_count") or 0
        except RuntimeError as exc:
            if "QueryCanceled" not in str(exc) or len(batch) < 2:
                raise
            half = len(batch) // 2
            run(batch[:half])
            run(batch[half:])

    for batch in chunked(items):
        run(batch)
    print(f"  {label}: {done:,} rows", flush=True)
    return done


def base_celex(celex):
    return DERIVED_SUFFIX.sub("", celex or "") or None


CELEX_LABEL = re.compile(r"^\s*(\d{5}[A-Z]{1,2}\d{4}(?:\(\d{2}\))?(?:_(?:SUM|RES|INF|EXT))?)")


def document_celex(entry):
    """CELEX of the work a text was taken from. Derived works (summaries,
    information sheets, extracts) keep the suffix of their own label; texts
    of a decision get the decision's CELEX without a derived suffix."""
    if entry["rule"] == "label":
        label = CELEX_LABEL.match(entry["head"] or "")
        if label:
            return label.group(1)
    return base_celex(entry["celex"] or None)


def capitalised_heading(head):
    for match in re.finditer(r"[^\W\d_]{2,}(?:\s+[^\W\d_]+){1,2}", (head or "")[:60]):
        if match.start() < 40 and all(word.isupper() for word in match.group(0).split()):
            return True
    return False


def carries_date(head, decision_date):
    if not decision_date:
        return False
    year, _, day = decision_date.split("-")
    opening = (head or "")[:300]
    return year in opening and re.search(rf"(?<!\d){int(day)}(?!\d)", opening) is not None


def load_production():
    cases, last = {}, 0
    while True:
        page = rows("""
            SELECT c.id, upper(c.ecli) AS ecli, c.celex_id, c.case_number,
                   c.date_decision::text AS decision_date, dt.code AS doctype
              FROM cases c JOIN cjeu_document d ON d.case_id = c.id
              LEFT JOIN document_type dt ON dt.id = c.document_type_id
             WHERE c.id > %s ORDER BY c.id LIMIT 1000""", [last])
        if not page:
            break
        for case in page:
            cases[case["ecli"]] = case
        last = page[-1]["id"]
    # md5() detoasts every text: shrink the page when a page of long texts
    # exceeds the statement timeout, and grow it back afterwards.
    texts, last, size = {}, 0, 500
    while True:
        try:
            page = rows(f"""
                SELECT ct.id, ct.case_id, lower(ct.language) AS language, ct.source,
                       md5(ct.fulltext) AS md5, ct.summary IS NOT NULL AS has_summary,
                       ct.document_kind
                  FROM case_text ct JOIN cjeu_document d ON d.case_id = ct.case_id
                 WHERE ct.id > %s ORDER BY ct.id LIMIT {size}""", [last])
        except RuntimeError as exc:
            if "QueryCanceled" not in str(exc) or size <= 10:
                raise
            size = max(10, size // 2)
            continue
        if not page:
            break
        for text in page:
            texts[text["id"]] = text
        last = page[-1]["id"]
        size = min(500, size * 2)
    return cases, texts


def build_plan(classification, cases, texts):
    by_id = {case["id"]: case for case in cases.values()}
    by_number = defaultdict(list)
    for case in cases.values():
        if case["case_number"]:
            by_number[case["case_number"]].append(case)
    by_base = {base_celex(case["celex_id"]): case for case in cases.values() if case["celex_id"]}
    slots = defaultdict(list)
    for text in texts.values():
        slots[(text["case_id"], text["language"])].append(text)

    classify, drops, moves, kept = [], [], [], Counter()
    claimed = set()
    for entry in classification:
        text = texts.get(int(entry["id"]))
        if text is None:
            continue
        kind, celex = entry["kind"], entry["celex"] or None
        classify.append((text["id"], kind, document_celex(entry)))
        if kind != "misfiled":
            continue
        home = by_id[text["case_id"]]
        owner = None
        if entry["rule"] == "corpus-other-ecli":
            owner = by_base.get(base_celex(celex))
        else:
            candidates = [
                case for case in by_number.get(home["case_number"], [])
                if case["id"] != home["id"] and carries_date(entry["head"], case["decision_date"])
            ]
            owner = candidates[0] if len(candidates) == 1 else None
        if owner is None:
            kept["owner unknown"] += 1
            continue
        there = slots.get((owner["id"], text["language"]), [])
        if any(other["md5"] == text["md5"] for other in there):
            drops.append(text["id"])
        elif any(other["source"] == text["source"] for other in there) or (
            owner["id"], text["language"], text["source"]) in claimed:
            kept["owner has another text from this source"] += 1
        else:
            claimed.add((owner["id"], text["language"], text["source"]))
            owner_kind = owner["doctype"] if owner["doctype"] in DECISION_KINDS else "other"
            new_kind = owner_kind if capitalised_heading(entry["head"]) else "oj_notice"
            moves.append((text["id"], owner["id"], new_kind, base_celex(owner["celex_id"])))

    leaving = set(drops) | {move[0] for move in moves}
    copy_summary, convert, keep_summary_row = [], [], []
    for text_id in sorted(leaving):
        text = texts[text_id]
        if not text["has_summary"]:
            continue
        remaining = [
            other for other in slots[(text["case_id"], text["language"])]
            if other["id"] not in leaving
        ]
        if remaining:
            target = min(remaining, key=lambda other: (other["document_kind"] == "misfiled",
                                                       other["md5"] is None, other["id"]))
            if not target["has_summary"]:
                copy_summary.append((target["id"], text_id))
                target["has_summary"] = True
        elif text_id in drops:
            convert.append(text_id)  # keep the row as a summary-only row
        else:
            keep_summary_row.append(text_id)  # recreate a summary-only row
    drops = [text_id for text_id in drops if text_id not in set(convert)]

    celex_fixes = []
    used = {case["celex_id"] for case in cases.values() if case["celex_id"]}
    for case in cases.values():
        old = case["celex_id"]
        new = base_celex(old)
        if old and new != old and new not in used:
            celex_fixes.append((case["id"], old, new))
            used.add(new)
    return {
        "classify": classify, "copy_summary": copy_summary, "convert": convert,
        "keep_summary_row": keep_summary_row, "drops": drops, "moves": moves,
        "kept": kept, "celex_fixes": celex_fixes,
    }


def archive(plan, archive_dir):
    archive_dir.mkdir(parents=True, exist_ok=True)
    text_ids = sorted(set(plan["drops"]) | set(plan["convert"]) | {m[0] for m in plan["moves"]})
    path = archive_dir / "case_text_rows.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as out:
        for batch in chunked(text_ids, 100):
            for row in rows("""
                SELECT id, case_id, language, fulltext, summary, summary_source, source,
                       text_format, missing_reasons, is_stub, document_kind, document_celex,
                       created_at::text, updated_at::text
                  FROM case_text WHERE id = ANY(%s::bigint[])""", [batch]):
                out.write(json.dumps(row, ensure_ascii=False) + "\n")
    with gzip.open(archive_dir / "case_celex_changes.jsonl.gz", "wt", encoding="utf-8") as out:
        for case_id, old, new in plan["celex_fixes"]:
            out.write(json.dumps({"case_id": case_id, "old": old, "new": new}) + "\n")
    print(f"  archived {len(text_ids):,} text rows and {len(plan['celex_fixes']):,} CELEX changes -> {archive_dir}")


def link_candidates(fixes):
    new_celex = [new for _, _, new in fixes]
    found = []
    for batch in chunked(new_celex, 500):
        found += rows("""
            SELECT cc.id, c.id AS target
              FROM case_citation cc JOIN cases c ON c.celex_id = cc.target_celex_raw
             WHERE cc.target_case_id IS NULL AND cc.source_dataset = 'cellar_sparql'
               AND cc.target_celex_raw = ANY(%s::text[])
               AND NOT EXISTS (
                   SELECT 1 FROM case_citation twin
                    WHERE twin.source_case_id = cc.source_case_id
                      AND twin.target_case_id = c.id
                      AND twin.relation_type = cc.relation_type
                      AND twin.source_dataset = cc.source_dataset)""", [batch])
    return [(row["id"], row["target"]) for row in found]


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "1") != "0"
    with open(os.environ["CLASSIFICATION_CSV"], newline="", encoding="utf-8") as handle:
        classification = list(csv.DictReader(handle))
    print("reading production ...", flush=True)
    cases, texts = load_production()
    plan = build_plan(classification, cases, texts)
    print("plan:")
    print(f"  classify            {len(plan['classify']):>8,}  {dict(Counter(k for _, k, _ in plan['classify']))}")
    print(f"  copy summary        {len(plan['copy_summary']):>8,}")
    print(f"  keep as summary row {len(plan['convert']) + len(plan['keep_summary_row']):>8,}")
    print(f"  drop duplicates     {len(plan['drops']):>8,}")
    print(f"  move to owner       {len(plan['moves']):>8,}")
    print(f"  keep misfiled       {sum(plan['kept'].values()):>8,}  {dict(plan['kept'])}")
    print(f"  fix case CELEX      {len(plan['celex_fixes']):>8,}")
    if dry_run:
        print("DRY RUN: nothing written")
        return 0

    archive(plan, Path(os.environ["ARCHIVE_DIR"]))
    print("writing:", flush=True)
    execute_chunks("classify", """
        UPDATE case_text ct SET document_kind = v.kind, document_celex = v.celex
          FROM unnest(%s::bigint[], %s::text[], %s::text[]) AS v(id, kind, celex)
         WHERE ct.id = v.id AND ct.document_kind IS NULL""", plan["classify"], 3)
    execute_chunks("copy summaries", """
        UPDATE case_text t SET summary = s.summary, summary_source = s.summary_source
          FROM unnest(%s::bigint[], %s::bigint[]) AS v(target, source)
          JOIN case_text s ON s.id = v.source
         WHERE t.id = v.target AND t.summary IS NULL""", plan["copy_summary"], 2)
    execute_chunks("keep summary-only rows", """
        UPDATE case_text SET fulltext = NULL, text_format = NULL,
               document_kind = NULL, document_celex = NULL
         WHERE id = ANY(%s::bigint[])""", [(i,) for i in plan["convert"]], 1)
    execute_chunks("drop duplicates", """
        DELETE FROM case_text WHERE id = ANY(%s::bigint[]) AND document_kind = 'misfiled'""",
        [(i,) for i in plan["drops"]], 1)
    move_ids = {move[0] for move in plan["moves"]}
    keep_rows = [move for move in plan["moves"] if move[0] in set(plan["keep_summary_row"])]
    execute_chunks("move to owner", """
        UPDATE case_text ct SET case_id = v.owner, document_kind = v.kind, document_celex = v.celex
          FROM unnest(%s::bigint[], %s::bigint[], %s::text[], %s::text[]) AS v(id, owner, kind, celex)
         WHERE ct.id = v.id AND ct.document_kind = 'misfiled'""", plan["moves"], 4)
    execute_chunks("recreate summary-only rows", """
        INSERT INTO case_text (case_id, language, source, summary, summary_source)
        SELECT v.home, t.language, t.source, t.summary, t.summary_source
          FROM unnest(%s::bigint[], %s::bigint[]) AS v(id, home) JOIN case_text t ON t.id = v.id
        ON CONFLICT (case_id, language, source) DO NOTHING""",
        [(move[0], texts[move[0]]["case_id"]) for move in keep_rows], 2)
    execute_chunks("clear summaries on moved rows", """
        UPDATE case_text SET summary = NULL, summary_source = NULL
         WHERE id = ANY(%s::bigint[]) AND summary IS NOT NULL""", [(i,) for i in move_ids], 1)
    execute_chunks("fix case CELEX", """
        UPDATE cases c SET celex_id = v.new
          FROM unnest(%s::bigint[], %s::text[], %s::text[]) AS v(id, old, new)
         WHERE c.id = v.id AND c.celex_id = v.old""", plan["celex_fixes"], 3)
    execute_chunks("fix cjeu_document CELEX", """
        UPDATE cjeu_document d SET celex_id = v.new
          FROM unnest(%s::bigint[], %s::text[], %s::text[]) AS v(id, old, new)
         WHERE d.case_id = v.id AND d.celex_id = v.old""", plan["celex_fixes"], 3)
    links = link_candidates(plan["celex_fixes"])
    print(f"  citations to link: {len(links):,}", flush=True)
    execute_chunks("link citations", """
        UPDATE case_citation cc SET target_case_id = v.target
          FROM unnest(%s::bigint[], %s::bigint[]) AS v(id, target)
         WHERE cc.id = v.id AND cc.target_case_id IS NULL""", links, 2)
    print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
