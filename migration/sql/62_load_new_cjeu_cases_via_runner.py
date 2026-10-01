#!/usr/bin/env python3
"""Load NEW CJEU cases (metadata, not texts) into cle_v2 through the sql-runner.

The runner-transport counterpart of 50_load_cjeu.py for corpus rows whose
ECLI has no CJEU satellite in production yet. For each such ECLI it writes
exactly what 50 writes for a case:

  cases, cjeu_document, cjeu_ag_opinion (opinion CELEXes),
  cjeu_national_document (sector 8), domain/case_domain, judge/case_judge,
  party/case_party, case_law_reference, case_citation (both directions),
  case_text summary rows (cases.parquet `summary`, as 50 stored them)

plus the lookup rows 50 creates on demand (language, procedure_type, domain,
judge, party). It never writes FULLTEXTS: 60_sync_cjeu_texts_via_runner.py
syncs texts afterwards, and 60 only sees ECLIs that already have
cases + cjeu_document rows. That ordering is the point of this loader.

Summaries (50's layout, verified live): the summary sits on the case_text
row of the procedure language (`language_procedure` -> ISO, 'en' when
absent) with summary_source = first token of `summary_source`; when no text
row exists for that language, 50 inserted a summary-only row with
source='CELLAR_ITEM', fulltext/text_format/missing_reasons NULL.
summary_tsv is a generated column; summary_embedding/embedding_model stay
NULL (production has no CJEU embeddings). New cases have no text rows yet,
so this loader inserts exactly that summary-only CELLAR_ITEM row. 60 then
attaches the fulltext to the SAME row instead of adding a sibling: for a
CELLAR_ITEM parquet row it sees a same-source row whose md5(fulltext) is
md5('') and updates it in place; for another CJEU source (e.g.
INFOCURIA_BLOB_HTML) it treats the CELLAR_ITEM row as the stale-source row
and upgrades it in place. Both of 60's UPDATEs set only source, fulltext,
text_format and missing_reasons, so the summary survives, and the
(case_id, language, source) key never sees a duplicate.

Citation linking (the only UPDATEs this script issues): existing
case_citation rows that are still unresolved (target_case_id IS NULL) and
whose raw target is a newly loaded case get target_case_id set, following
the conventions of the loader that created them:
  cellar_sparql (by target_celex_raw): raw CELEX kept, is_cross_jurisdiction
      unchanged (false), as 50/59 store resolved rows;
  rs_* (by target_ecli_raw): target_ecli_raw cleared, is_cross_jurisdiction =
      NOT target.sources @> '{RS}', as 40_citations does;
  echr_edge (by target_ecli_raw): target_ecli_raw cleared, cross = false (40).
Rows of other datasets are only reported. Updates go by explicit id lists
(<= LINK_CHUNK ids per statement, well under the 10k-row cap), re-check
target_case_id IS NULL, skip a row whose resolved twin already exists, and
are split in half on timeout/row-cap errors. trg_case_citation_counts fires
AFTER UPDATE and increments the new target's cited_by_count (the source's
cites_count already counted the unresolved row).

Safety properties:

- DRY_RUN=1 is the default. A dry run only issues /query (read-only txn)
  requests and prints the plan; the transport refuses /execute outright.
- Insert-only apart from citation linking. Every other statement is
  INSERT ... SELECT FROM unnest(...) with ON CONFLICT DO NOTHING or a NOT
  EXISTS guard; nothing is DELETEd. The only other (indirect) update is the existing
  trg_cjeu_document_sources_attach trigger appending 'CJEU' to
  cases.sources when a cjeu_document attaches to a pre-existing (RS-origin)
  cases row, which is the schema's own D13 contract.
- Idempotent and resumable. "New" means "no cjeu_document row yet", and
  cjeu_document is written LAST for each batch, after every other
  satellite of that batch. A crash therefore leaves the affected ECLIs
  "new", and the rerun redoes their (conflict-free) inserts and links (the
  link UPDATE only touches rows that are still unresolved).
- case_citation_counts is maintained by the trg_case_citation_counts
  trigger (verified live), so no separate counts pass is needed.
- cases.id is GENERATED ALWAYS AS IDENTITY: it is never supplied; every
  satellite resolves its case_id server-side by joining cases.ecli.

Env:
  SQL_RUNNER_URL        e.g. https://demo-psql.caselawexplorer.tech
  SQL_RUNNER_TOKEN      HMAC token (never printed)
  SQL_RUNNER_CONFIRM    default execute-cle-v2
  CASES_PARQUET         local cases.parquet; downloaded from HF when unset
  CITATIONS_PARQUET     optional extra edge list (columns source_celex,
                        target_celex, relation_type); edges touching new
                        cases are merged with the cases.parquet edges
  DRY_RUN               default 1; set DRY_RUN=0 to write
  SKIP_INVALID          default 0; with 1, rows failing validation are
                        skipped instead of aborting a write run
  ERRORS_TSV            optional path: write every validation error (ecli, error)
  CASE_BATCH            cases per satellite batch (default 400)
  ROW_CHUNK             max rows per INSERT statement (default 1000)
  LIMIT_NEW             optional cap on the number of new cases (testing)
  SKIP_SUMMARIES        default 0; 1 skips the case_text summary rows
  SKIP_CITATION_LINKING default 0; 1 skips resolving existing unresolved
                        case_citation rows that point at the new cases
  LINK_CHUNK            ids per linking UPDATE (default 1000)
  INCLUDE_CJEU_KEYWORD  default 0; 1 also maps `keywords` to domain scheme
                        cjeu_keyword as 50's code does (production has none)
  SAMPLE_ROWS           mapped rows printed in the plan (default 3)
  EXPLAIN_SQL           dry run only: 1 plans every INSERT with EXPLAIN (no
                        ANALYZE) via /query, validating SQL against the live
                        schema with real parameters; never executes
  PARITY_SAMPLE         optional N: in a dry run, compare the mapping of N
                        existing production CJEU cases against their live rows
"""

from __future__ import annotations

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
from dataclasses import dataclass, field

import pandas as pd

HF_REPO = "davidwickerhf/cjeu-opendata"

# ---------------------------------------------------------------------------
# Mapping tables: copied verbatim from 50_load_cjeu.py (the authoritative
# column mapping). Keep them in sync if 50 ever changes.
# ---------------------------------------------------------------------------

CDM_ROLE_COLUMNS = {
    "legal_resource": "legal_basis",
    "based_on_treaty": "based_on_treaty",
    "affecting_string": "affects",
    "case_law_amends_resource_legal": "amends",
    "case_law_amends_by_correction_resource_legal": "amends_by_correction",
    "case_law_confirms_resource_legal": "confirms",
    "case_law_declares_void_resource_legal": "declares_void",
    "case_law_declares_void_by_preliminary_ruling_resource_legal": "declares_void_by_preliminary_ruling",
    "case_law_incidentally_declares_void_resource_legal": "incidentally_declares_void",
    "case_law_declares_valid_resource_legal": "declares_valid",
    "case_law_declares_incidentally_valid_resource_legal": "declares_incidentally_valid",
    "case_law_states_failure_concerning_resource_legal": "states_failure",
    "case_law_suspends_application_of_resource_legal": "suspends_application",
    "case_law_immediately_enforces_resource_legal": "immediately_enforces",
    "resource_legal_incorporates_resource_legal": "incorporates",
    "resource_legal_corrects_resource_legal": "corrects",
}

CITE_COLUMNS = {
    "citing": "cites",
    "work_cites_work": "cites",
    "cited_by": "cited_by",
    "case_law_joins_case_court": "joins",
    "case_law_subject_to_appeal_in_case_court": "subject_to_appeal",
    "case_law_reexamined_by_case_court": "reexamined_by",
    "case_law_referred_to_for_preliminary_ruling_case_law": "referred_for_preliminary_ruling",
    "case_law_is_about_concept_case_law": "is_about_concept",
    "case_law_is_about_concept_new_case_law": "is_about_concept",
    "work_is_logical_successor_of_work": "logical_successor_of",
}

DOMAIN_COLUMNS = {
    "subject_matter": "cjeu_subject_matter",
    "eurovoc": "eurovoc",
    "keywords": "cjeu_keyword",
    "directory_codes": "cjeu_directory_code",
}

NATIONAL_COLUMNS = [
    ("case_law_delivered_by_court_national", "national_court_uri"),
    ("case_law_national_decision_internal_identifier", "national_decision_internal_id"),
    ("case_law_national_parties", "national_parties_raw"),
    ("case_law_national_keywords", "national_keywords"),
    ("case_law_national_reference_publication", "national_reference_publication"),
    ("case_law_national_reference_publication_conclusion", "national_reference_publication_conclusion"),
    ("case_law_national_follow_up", "national_follow_up"),
    ("case_law_national_judgement_reference", "national_judgement_reference"),
    ("case_law_national_act_reference_national", "national_act_reference_national"),
    ("case_law_national_act_reference_international", "national_act_reference_international"),
    ("case_law_national_act_reference_european", "national_act_reference_european"),
    ("case_law_national_based_on_resource_legal", "national_based_on_resource_legal"),
]
NATIONAL_TARGETS = [t for _, t in NATIONAL_COLUMNS]

AGENT_COLUMNS = (
    ("case_law_defended_by_agent", "defendant_agent"),
    ("case_law_requested_by_agent", "applicant_agent"),
    ("commented_by_agent", "commenting_agent"),
)

LANG_NAME_TO_ISO = {
    "english": "en", "french": "fr", "german": "de", "italian": "it",
    "spanish": "es", "dutch": "nl", "polish": "pl", "greek": "el",
    "portuguese": "pt", "romanian": "ro", "bulgarian": "bg", "swedish": "sv",
    "danish": "da", "finnish": "fi", "slovak": "sk", "slovenian": "sl",
    "croatian": "hr", "estonian": "et", "latvian": "lv", "lithuanian": "lt",
    "maltese": "mt", "irish": "ga", "hungarian": "hu", "czech": "cs",
}

FORMATION_WORDS = [
    ("GC", ("grand", "grande")), ("FC", ("full court", "plén", "assembl")),
    ("1C", ("first", "première")), ("2C", ("second", "deuxième")),
    ("3C", ("third", "troisième")), ("4C", ("fourth", "quatrième")),
    ("5C", ("fifth", "cinquième")), ("6C", ("sixth", "sixième")),
    ("7C", ("seventh", "septième")), ("8C", ("eighth", "huitième")),
    ("9C", ("ninth", "neuvième")), ("10C", ("tenth", "dixième")),
]

SCALAR_COLUMNS = [
    "ecli", "celex", "date_publication", "work_title", "language_procedure",
    "judicial_procedure_type", "delivered_by_court_formation", "sector",
    "type_procedure", "date_of_request", "references_journals",
    "case_law_published_in_erecueil", "local_identifier", "work_part_of_dossier",
    "citations_extra_info", "national_judgement",
    "case_law_is_about_case_law_subject_matter", "origin_country_or_role_qualifier",
    "judge_rapporteur", "case_law_delivered_by_judge", "origin_country",
    "advocate_general", "conclusions", "opinion_advocate_general_joined_to_case_court",
    "summary", "summary_source",
]
NEEDED_COLUMNS = list(dict.fromkeys(
    SCALAR_COLUMNS + list(DOMAIN_COLUMNS) + [c for c, _ in AGENT_COLUMNS]
    + list(CITE_COLUMNS) + list(CDM_ROLE_COLUMNS) + [c for c, _ in NATIONAL_COLUMNS]
))


# ---------------------------------------------------------------------------
# Value helpers (same semantics as 50_load_cjeu.py)
# ---------------------------------------------------------------------------

def _isna(v) -> bool:
    if v is None:
        return True
    try:
        return bool(pd.isna(v))
    except (TypeError, ValueError):
        return False


def toks(v):
    if _isna(v):
        return []
    return [t.strip() for t in str(v).split(";") if t.strip()]


def first(v):
    t = toks(v)
    return t[0] if t else None


def whole(v):
    """Full cell, NOT ';'-tokenized (summaries contain semicolons)."""
    if _isna(v):
        return None
    s = str(v).strip()
    return s or None


# CELEX shape check. Sector-6 and the sector-8 national CELEXes both match it
# (all 1,795 live sector-8 values, e.g. 82025SI1007(51), do); the derived-work
# suffixes the corpus sometimes carries on the first token are stripped first.
CELEX_RE = re.compile(r"^\d{5}[A-Z]{1,2}\d{4}(\(\d{2}\))?$")
CELEX_DERIVED_SUFFIX = re.compile(r"_(SUM|RES|INF)$")


def celex_well_formed(celex):
    return bool(celex) and bool(CELEX_RE.match(CELEX_DERIVED_SUFFIX.sub("", celex)))


def raw(v):
    """Untokenized, unstripped cell (50 passed these columns through as-is)."""
    return None if _isna(v) else str(v)


def norm_lang(v):
    w = (first(v) or "").lower()
    return LANG_NAME_TO_ISO.get(w, w if len(w) == 2 else None)


def norm_ecli(v):
    return str(v).strip().upper() if not _isna(v) else None


def celex_kind(celex):
    m = re.match(r"^[68]\d{4}([A-Z])([A-Z])", celex or "")
    if not m:
        return None, "other", False
    court = {"C": "CJEU", "T": "EGC", "F": "CST"}.get(m.group(1))
    dt = {"J": "judgment", "O": "order", "C": "opinion", "V": "ruling",
          "D": "decision"}.get(m.group(2), "other")
    return court, dt, m.group(2) == "C"


def case_number(celex):
    m = re.match(r"^[68](\d{4})([A-Z])[A-Z](\d{4})", celex or "")
    if not m:
        return None
    return f"{ {'C': 'C', 'T': 'T', 'F': 'F'}.get(m.group(2), 'C') }-{int(m.group(3))}/{m.group(1)[2:]}"


def formation_code(raw_formation):
    f = (raw_formation or "").lower()
    for code, words in FORMATION_WORDS:
        if any(w in f for w in words):
            return code
    return None


def importance(formation_raw):
    code = formation_code(formation_raw)
    if code in ("GC", "FC"):
        return 1
    if code in ("1C", "2C", "3C", "4C", "5C"):
        return 2
    if code:
        return 3
    return 4 if formation_raw else None


def parse_date(v):
    """ISO date string or None (NaT/unparseable -> None, like COPY NULL)."""
    if not v:
        return None
    d = pd.to_datetime(v, errors="coerce", utc=True)
    if pd.isna(d):
        return None
    return d.date().isoformat()


def decision_date(date_publication):
    t = toks(date_publication)
    return parse_date(min(t)) if t else None


# ---------------------------------------------------------------------------
# Row mapping
# ---------------------------------------------------------------------------

@dataclass
class CaseMapping:
    ecli: str
    celex: str
    case: dict
    document: dict
    ag: dict | None = None
    national: dict | None = None
    domains: set = field(default_factory=set)      # (scheme, name)
    judges: set = field(default_factory=set)       # (full_name, role)
    parties: set = field(default_factory=set)      # (canonical_name, role_class, role)
    lawrefs: set = field(default_factory=set)      # (raw_resource, role)
    citations: set = field(default_factory=set)    # (target_celex, relation_type)
    summary: dict | None = None                    # case_text summary row


def map_row(d, include_keywords=False) -> CaseMapping:
    """Map one corpus row to the rows 50_load_cjeu.py (+55) produce for it.

    ``keywords`` -> scheme ``cjeu_keyword`` is in 50's DOMAIN_COLUMNS but
    production has zero cjeu_keyword domains (verified live; the column is a
    duplicate of ``eurovoc`` in 45,524/45,524 rows of the load-time corpus),
    so it is skipped unless ``include_keywords`` (INCLUDE_CJEU_KEYWORD=1).
    """
    get = d.get
    ecli = norm_ecli(get("ecli"))
    celex = first(get("celex"))
    court, dtcode, is_opinion = celex_kind(celex)
    form_raw = first(get("delivered_by_court_formation"))
    cn = case_number(celex)
    title = first(get("work_title"))
    sector = first(get("sector"))
    m = CaseMapping(
        ecli=ecli,
        celex=celex,
        case={
            "ecli": ecli,
            "celex": celex,
            "title": title or (f"Case {cn}" if cn else None),
            "date_decision": decision_date(get("date_publication")),
            "court_code": court or "CJEU",
            "lang": norm_lang(get("language_procedure")),
            "doctype": dtcode,
            "proc": first(get("judicial_procedure_type")),
            "case_number": cn,
            "importance": importance(form_raw),
        },
        document={
            "ecli": ecli,
            "celex": celex,
            "sector": sector,
            "case_number": cn,
            "formation_code": formation_code(form_raw),
            "proc_type": first(get("type_procedure")),
            "date_lodged": parse_date(first(get("date_of_request"))),
            "journal_refs": first(get("references_journals")),
            "erecueil_ref": first(get("case_law_published_in_erecueil")),
            "local_identifier": first(get("local_identifier")),
            "dossier_uri": first(get("work_part_of_dossier")),
            "citations_extra_info": raw(get("citations_extra_info")),
            "national_judgement_xml": raw(get("national_judgement")),
        },
    )
    if is_opinion:
        m.ag = {
            "ecli": ecli,
            "ag": first(get("advocate_general")),
            "opinion_uri": first(get("conclusions")),
            "parent_raw": first(get("opinion_advocate_general_joined_to_case_court")),
        }
    if (summary := whole(get("summary"))):
        m.summary = {"ecli": ecli, "lang": norm_lang(get("language_procedure")) or "en",
                     "summary": summary, "summary_source": first(get("summary_source"))}
    if sector == "8":
        nat = {t: first(get(c)) for c, t in NATIONAL_COLUMNS}
        if any(v is not None for v in nat.values()):
            m.national = {"ecli": ecli, **nat}

    for t in toks(get("case_law_is_about_case_law_subject_matter")):
        m.domains.add(("cjeu_is_about_subject", t))
    for col, scheme in DOMAIN_COLUMNS.items():
        if col == "keywords" and not include_keywords:
            continue
        for t in toks(get(col)):
            m.domains.add((scheme, t))

    if (jr := first(get("judge_rapporteur"))):
        m.judges.add((jr, "rapporteur"))
    for j in toks(get("case_law_delivered_by_judge")):
        m.judges.add((j, "judge"))

    # Parties, matching the live representation: 50 linked origin_country
    # and the agent columns to role_class='agent' parties; 55 linked the
    # origin_country_or_role_qualifier tokens to role_class='state' parties.
    for col, role in AGENT_COLUMNS:
        for p in toks(get(col)):
            m.parties.add((p, "agent", role))
    if (oc := first(get("origin_country"))):
        m.parties.add((oc, "agent", "referring_state"))
    for t in toks(get("origin_country_or_role_qualifier")):
        m.parties.add((t, "state", "referring_state"))

    for col, rel in CITE_COLUMNS.items():
        for t in toks(get(col)):
            m.citations.add((t, rel))
    for col, role in CDM_ROLE_COLUMNS.items():
        for t in toks(get(col)):
            m.lawrefs.add((t, role))
    return m


def load_corpus(path) -> pd.DataFrame:
    """Read and normalize cases.parquet the way 50 does (ecli+celex required,
    dedup by ECLI keeping the first row)."""
    import pyarrow.parquet as pq

    available = set(pq.ParquetFile(path).schema_arrow.names)
    cols = [c for c in NEEDED_COLUMNS if c in available]
    missing = sorted(set(NEEDED_COLUMNS) - available)
    if "ecli" not in available or "celex" not in available:
        raise SystemExit(f"{path}: ecli/celex columns missing")
    if missing:
        print(f"note: corpus lacks {len(missing)} mapped columns (treated as empty): {missing}")
    df = pd.read_parquet(path, columns=cols)
    for c in missing:
        df[c] = None
    df["x_celex"] = df["celex"].map(first)
    df = df[df["ecli"].notna() & df["x_celex"].notna()]
    df = df.drop_duplicates(subset=["ecli"])
    df["x_ecli"] = df["ecli"].map(norm_ecli)
    df = df.drop_duplicates(subset=["x_ecli"])
    return df.reset_index(drop=True)


def load_extra_citations(path):
    """Optional CITATIONS_PARQUET: edge list keyed by source CELEX."""
    df = pd.read_parquet(path)
    need = {"source_celex", "target_celex", "relation_type"}
    if not need.issubset(df.columns):
        raise SystemExit(f"{path}: CITATIONS_PARQUET needs columns {sorted(need)}, got {list(df.columns)}")
    edges = defaultdict(set)
    for s, t, r in zip(df["source_celex"], df["target_celex"], df["relation_type"]):
        if s and t and r:
            edges[str(s).strip()].add((str(t).strip(), str(r).strip()))
    return edges


# ---------------------------------------------------------------------------
# Runner transport (HMAC signing as in 59/60)
# ---------------------------------------------------------------------------

class RunnerError(RuntimeError):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:500]}")
        self.status = status
        self.body = body

    @property
    def splittable(self):
        return self.status in (400, 403) and (
            "QueryCanceled" in self.body or "write_row_limit_exceeded" in self.body
            or "statement timeout" in self.body)


class DryRunViolation(RuntimeError):
    pass


class Runner:
    def __init__(self, url, token, confirm="execute-cle-v2", dry_run=True,
                 retries=5, sleep=time.sleep):
        self.url = url.rstrip("/")
        self._token = token
        self.confirm = confirm
        self.dry_run = dry_run
        self.retries = retries
        self.sleep = sleep
        self.calls = Counter()

    def query(self, sql, params=None):
        return self._post("query", sql, params)

    def execute(self, sql, params=None):
        if self.dry_run:
            raise DryRunViolation("execute() called in DRY_RUN mode")
        return self._post("execute", sql, params)

    def _send(self, endpoint, body):  # separated for tests
        url = f"{self.url}/{endpoint}"
        ts, nonce = str(int(time.time())), secrets.token_hex(16)
        msg = "\n".join(["POST", urllib.parse.urlparse(url).path, ts, nonce,
                         hashlib.sha256(body).hexdigest()]).encode()
        sig = hmac.new(self._token.encode(), msg, hashlib.sha256).hexdigest()
        req = urllib.request.Request(url, data=body, method="POST", headers={
            "Content-Type": "application/json",
            "X-SQL-Runner-Timestamp": ts, "X-SQL-Runner-Nonce": nonce,
            "X-SQL-Runner-Signature": f"v1={sig}"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    def _post(self, endpoint, sql, params):
        payload = {"sql": sql}
        if params is not None:
            payload["params"] = params
        if endpoint == "execute":
            payload["confirm"] = self.confirm
        body = json.dumps(payload, separators=(",", ":"), default=str).encode()
        self.calls[endpoint] += 1
        last = None
        for attempt in range(self.retries):
            try:
                out = self._send(endpoint, body)
                if not out.get("ok"):
                    raise RuntimeError(str(out)[:300])
                return out
            except urllib.error.HTTPError as exc:
                err = RunnerError(exc.code, exc.read().decode(errors="replace"))
                # 4xx are deterministic (timeout, row cap, SQL error): let the
                # caller shrink/bisect instead of hammering the same statement
                if 400 <= exc.code < 500 and exc.code not in (401, 408, 429):
                    raise err from None
                last = err
            except Exception as exc:  # network / 5xx / malformed
                last = exc
            self.sleep(3 * (attempt + 1))
        raise last


def paginated(runner, sql, params=(), key="id"):
    """Keyset-paginate a read; sql's last placeholder is the key cursor."""
    last = 0
    while True:
        rows = runner.query(sql, [*params, last])["rows"]
        if not rows:
            return
        yield from rows
        last = rows[-1][key]


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def query_any(runner, sql, values, chunk=900):
    """Run `sql` (one %s text[] placeholder) over values in result-cap-safe chunks."""
    out = []
    for part in chunks(sorted(values), chunk):
        res = runner.query(sql, [part])
        if res.get("truncated"):
            raise RuntimeError("read truncated at the 1000-row cap; lower the chunk")
        out.extend(res["rows"])
    return out


# ---------------------------------------------------------------------------
# Production state
# ---------------------------------------------------------------------------

@dataclass
class ProdState:
    cjeu_eclis: dict                 # ecli -> case_id (cases JOIN cjeu_document)
    courts: set
    doctypes: set
    formations: set
    languages: set
    procedures: set
    judges: set
    parties: set                     # (name, role_class)
    domains: set                     # (scheme, name)


def read_prod_state(runner) -> ProdState:
    cjeu = {}
    for r in paginated(runner, """
            SELECT c.id, c.ecli FROM cases c JOIN cjeu_document d ON d.case_id = c.id
            WHERE c.id > %s ORDER BY c.id LIMIT 900"""):
        cjeu[r["ecli"]] = r["id"]

    def col(sql):
        return list(paginated(runner, sql))

    return ProdState(
        cjeu_eclis=cjeu,
        courts={r["code"] for r in col("SELECT id, code FROM court WHERE code IN ('CJEU','EGC','CST') AND id > %s ORDER BY id LIMIT 900")},
        doctypes={r["code"] for r in col("SELECT id, code FROM document_type WHERE id > %s ORDER BY id LIMIT 900")},
        formations={r["code"] for r in col("SELECT id, code FROM court_formation WHERE id > %s ORDER BY id LIMIT 900")},
        languages=_languages(runner),
        procedures={r["code"] for r in col("SELECT id, code FROM procedure_type WHERE id > %s ORDER BY id LIMIT 900")},
        judges={r["full_name"] for r in col("SELECT id, full_name FROM judge WHERE id > %s ORDER BY id LIMIT 900")},
        parties={(r["canonical_name"], r["role_class"]) for r in col(
            "SELECT id, canonical_name, role_class FROM party WHERE id > %s ORDER BY id LIMIT 900")},
        domains={(r["scheme"], r["name"]) for r in col(
            "SELECT id, scheme, name FROM domain WHERE id > %s ORDER BY id LIMIT 900")},
    )


def _languages(runner):
    out, last = set(), ""
    while True:
        rows = runner.query("SELECT iso_code FROM language WHERE iso_code > %s "
                            "ORDER BY iso_code LIMIT 900", [last])["rows"]
        if not rows:
            return out
        out.update(r["iso_code"] for r in rows)
        last = rows[-1]["iso_code"]


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    new: list                        # CaseMapping for cases to load
    attach_existing: dict            # ecli -> existing non-CJEU cases row (id, sources)
    incoming: list                   # (source_ecli, target_celex, relation)
    errors: list                     # (ecli, message) -> rows excluded
    warnings: list
    lookups: dict                    # table -> sorted list of new values
    stats: dict
    links: list = field(default_factory=list)       # LinkCandidate
    unlinkable: Counter = field(default_factory=Counter)


def build_plan(df, prod: ProdState, runner, extra_edges=None, limit=None,
               include_keywords=False) -> Plan:
    errors, warnings = [], []
    corpus_eclis = set(df["x_ecli"])
    missing_from_corpus = sorted(set(prod.cjeu_eclis) - corpus_eclis)

    new_df = df[~df["x_ecli"].isin(prod.cjeu_eclis.keys())]
    if limit:
        new_df = new_df.head(limit)
    new = [map_row(r, include_keywords) for r in new_df.to_dict("records")]
    if extra_edges:
        for m in new:
            m.citations |= extra_edges.get(m.celex, set())

    # ECLIs already present in `cases` without a CJEU satellite (cross-corpus
    # attach, like the 175 RS-origin Dutch sector-8 rows)
    existing_rows = query_any(runner,
        "SELECT id, ecli, celex_id, sources FROM cases WHERE ecli = ANY(%s::text[])",
        [m.ecli for m in new])
    attach = {r["ecli"]: r for r in existing_rows}

    # CELEX uniqueness: cases.celex_id is UNIQUE, and ON CONFLICT DO NOTHING
    # would silently drop the row, so collisions are validation errors.
    celex_rows = query_any(runner,
        "SELECT id, ecli, celex_id FROM cases WHERE celex_id = ANY(%s::text[])",
        [m.celex for m in new])
    celex_owner = {r["celex_id"]: r["ecli"] for r in celex_rows}
    # Several new ECLIs sharing one CELEX: which ECLI owns it is ambiguous,
    # so the whole group is rejected (no arbitrary first-row-wins).
    by_celex = defaultdict(list)
    for m in new:
        by_celex[m.celex].append(m.ecli)
    valid = []
    for m in new:
        if not celex_well_formed(m.celex):
            errors.append((m.ecli, f"malformed CELEX {m.celex!r}"))
            continue
        owner = celex_owner.get(m.celex)
        if owner and owner != m.ecli:
            errors.append((m.ecli, f"CELEX {m.celex} already belongs to production case {owner}"))
            continue
        if len(by_celex[m.celex]) > 1:
            others = [e for e in by_celex[m.celex] if e != m.ecli]
            errors.append((m.ecli, f"CELEX {m.celex} shared with other new corpus rows {others[:3]}"))
            continue
        if not m.ecli.startswith("ECLI:"):
            warnings.append(f"{m.ecli}: non-standard ECLI kept verbatim (as 50 does; "
                            f"production already stores such values)")
        if m.case["date_decision"] is None:
            warnings.append(f"{m.ecli}: no parseable date_publication -> date_decision NULL")
        if m.ecli in attach and m.celex and attach[m.ecli].get("celex_id") not in (None, m.celex):
            warnings.append(f"{m.ecli}: existing cases row has celex_id "
                            f"{attach[m.ecli]['celex_id']} (corpus {m.celex}); left unchanged")
        valid.append(m)
    new = valid

    # incoming edges: existing CJEU cases whose corpus row points at a new CELEX
    new_celex = resolvable_new_celex(new, attach)
    incoming = set()
    existing_df = df[df["x_ecli"].isin(prod.cjeu_eclis.keys())]
    cite_cols = [c for c in CITE_COLUMNS if c in existing_df.columns]
    for rec in existing_df[["x_ecli", "x_celex", *cite_cols]].to_dict("records"):
        for col in cite_cols:
            for t in toks(rec[col]):
                if t in new_celex:
                    incoming.add((rec["x_ecli"], t, CITE_COLUMNS[col]))
        if extra_edges:
            for t, r in extra_edges.get(rec["x_celex"], ()):
                if t in new_celex:
                    incoming.add((rec["x_ecli"], t, r))

    lookups = {
        "language": sorted(_languages_needed(new) - prod.languages),
        "procedure_type": sorted({m.case["proc"] for m in new if m.case["proc"]} - prod.procedures),
        "judge": sorted({n for m in new for n, _ in m.judges} - prod.judges),
        "party": sorted({(n, rc) for m in new for n, rc, _ in m.parties} - prod.parties),
        "domain": sorted({d for m in new for d in m.domains} - prod.domains),
        "court (unmapped)": sorted({m.case["court_code"] for m in new} - prod.courts),
        "document_type (unmapped)": sorted({m.case["doctype"] for m in new} - prod.doctypes),
        "court_formation (unmapped)": sorted(
            {m.document["formation_code"] for m in new if m.document["formation_code"]}
            - prod.formations),
    }

    stats = {
        "corpus_rows": len(df),
        "production_cjeu_cases": len(prod.cjeu_eclis),
        "production_cjeu_missing_from_corpus": len(missing_from_corpus),
        "production_cjeu_missing_from_corpus_sample": missing_from_corpus[:10],
        "new_eclis": len(new) + len(errors),
    }
    return Plan(new=new, attach_existing=attach, incoming=sorted(incoming),
                errors=errors, warnings=warnings, lookups=lookups, stats=stats)


def _languages_needed(new):
    langs = {m.case["lang"] for m in new if m.case["lang"]}
    return langs | {m.summary["lang"] for m in new if m.summary}


LINK_CELEX_DATASETS = {"cellar_sparql"}


def _link_kind(dataset, key_kind):
    """Which resolution rule applies to an unresolved row, or None."""
    if key_kind == "celex" and dataset in LINK_CELEX_DATASETS:
        return "celex"
    if key_kind == "ecli" and (dataset.startswith("rs_") or dataset == "echr_edge"):
        return "ecli"
    return None


@dataclass(frozen=True)
class LinkCandidate:
    id: int
    kind: str          # 'celex' | 'ecli'
    key: str           # raw target (CELEX or ECLI) = a new case's identifier
    dataset: str
    relation: str


def _read_by_keys(runner, sql, keys, chunk=100):
    """Read rows for key lists, halving a chunk if the 1000-row cap truncates it."""
    out = []
    pending = list(chunks(sorted(keys), chunk))
    while pending:
        part = pending.pop()
        res = runner.query(sql, [part])
        if res.get("truncated"):
            if len(part) == 1:
                raise RuntimeError(f"more than 1000 unresolved rows for {part[0]}")
            half = len(part) // 2
            pending += [part[:half], part[half:]]
            continue
        out.extend(res["rows"])
    return out


def collect_link_candidates(runner, plan):
    """Existing unresolved case_citation rows whose raw target is a new case
    (read-only). Fills plan.links and plan.unlinkable."""
    new_celex = resolvable_new_celex(plan.new, plan.attach_existing)
    new_ecli = {m.ecli for m in plan.new}
    found = []
    for r in _read_by_keys(runner, """
            SELECT id, target_celex_raw AS key, source_dataset, relation_type
            FROM case_citation
            WHERE target_case_id IS NULL AND target_celex_raw = ANY(%s::text[])""", new_celex):
        found.append((r, "celex"))
    for r in _read_by_keys(runner, """
            SELECT id, target_ecli_raw AS key, source_dataset, relation_type
            FROM case_citation
            WHERE target_case_id IS NULL AND target_ecli_raw = ANY(%s::text[])""", new_ecli):
        found.append((r, "ecli"))
    links, unlinkable, seen = [], Counter(), set()
    for r, key_kind in found:
        kind = _link_kind(r["source_dataset"], key_kind)
        if kind is None:
            unlinkable[f"{r['source_dataset']}:{r['relation_type']}:{key_kind}"] += 1
        elif r["id"] not in seen:
            seen.add(r["id"])
            links.append(LinkCandidate(r["id"], kind, r["key"], r["source_dataset"],
                                       r["relation_type"]))
    plan.links = sorted(links, key=lambda c: c.id)
    plan.unlinkable = unlinkable
    return plan.links


def link_counts(plan):
    return dict(sorted(Counter(f"{c.dataset}:{c.relation}" for c in plan.links).items()))


def resolvable_new_celex(new, attach):
    """CELEXes that cases.celex_id will carry after the load. A satellite that
    attaches to a pre-existing row keeps that row's celex_id (never updated),
    so it only resolves by CELEX when the existing value already matches."""
    return {m.celex for m in new
            if m.ecli not in attach or attach[m.ecli].get("celex_id") == m.celex}


def citation_resolution(runner, plan: Plan):
    """Resolved/unresolved split for the planned edges (read-only)."""
    new_celex = resolvable_new_celex(plan.new, plan.attach_existing)
    targets = {t for m in plan.new for t, _ in m.citations}
    known = {r["celex_id"] for r in query_any(
        runner, "SELECT celex_id FROM cases WHERE celex_id = ANY(%s::text[])",
        targets - new_celex)}
    resolvable = known | new_celex
    out_edges = [(m.ecli, t, r) for m in plan.new for t, r in sorted(m.citations)]
    resolved = sum(1 for _, t, _ in out_edges if t in resolvable)

    # incoming candidates already stored unresolved are skipped by SQL_CITE_IN;
    # the linking step resolves those rows instead
    stored = set()
    incoming_targets = sorted({t for _, t, _ in plan.incoming})
    for part in chunks(incoming_targets, 100):
        res = runner.query("""
            SELECT s.ecli, cc.target_celex_raw AS target, cc.relation_type AS rel
            FROM case_citation cc JOIN cases s ON s.id = cc.source_case_id
            WHERE cc.target_case_id IS NULL AND cc.source_dataset = 'cellar_sparql'
              AND cc.target_celex_raw = ANY(%s::text[])""", [part])
        if res.get("truncated"):
            raise RuntimeError("incoming-edge check truncated; lower the chunk")
        stored |= {(r["ecli"], r["target"], r["rel"]) for r in res["rows"]}
    incoming_new = [e for e in plan.incoming if e not in stored]
    return {
        "outgoing_edges": len(out_edges),
        "outgoing_resolved": resolved,
        "outgoing_unresolved": len(out_edges) - resolved,
        "incoming_edges_candidate": len(plan.incoming),
        "incoming_edges_already_stored_unresolved (skipped)": len(plan.incoming) - len(incoming_new),
        "incoming_edges_to_insert": len(incoming_new),
    }


def planned_counts(plan: Plan):
    new = plan.new
    return {
        "cases (new rows)": sum(1 for m in new if m.ecli not in plan.attach_existing),
        "cases (existing row, CJEU satellite attaches)": sum(1 for m in new if m.ecli in plan.attach_existing),
        "cjeu_document": len(new),
        "cjeu_ag_opinion": sum(1 for m in new if m.ag),
        "cjeu_national_document": sum(1 for m in new if m.national),
        "case_domain": sum(len(m.domains) for m in new),
        "case_judge": sum(len(m.judges) for m in new),
        "case_party": sum(len(m.parties) for m in new),
        "case_law_reference": sum(len(m.lawrefs) for m in new),
        "case_citation (outgoing)": sum(len(m.citations) for m in new),
        "case_citation (incoming, upper bound)": len(plan.incoming),
        "case_text (summary-only rows; no fulltext)": sum(1 for m in new if m.summary),
    }


# ---------------------------------------------------------------------------
# Writes (only reachable with DRY_RUN=0)
# ---------------------------------------------------------------------------

SQL_LANGUAGE = """
INSERT INTO language (iso_code, name)
SELECT DISTINCT v, v FROM unnest(%s::text[]) AS v
ON CONFLICT (iso_code) DO NOTHING"""

SQL_PROCEDURE = """
INSERT INTO procedure_type (code, name)
SELECT DISTINCT v, v FROM unnest(%s::text[]) AS v
ON CONFLICT (code) DO NOTHING"""

SQL_JUDGE = """
INSERT INTO judge (full_name)
SELECT DISTINCT v FROM unnest(%s::text[]) AS v
WHERE NOT EXISTS (SELECT 1 FROM judge j WHERE j.full_name = v)"""

SQL_PARTY = """
INSERT INTO party (canonical_name, role_class)
SELECT DISTINCT v.name, v.rc FROM unnest(%s::text[], %s::text[]) AS v(name, rc)
WHERE NOT EXISTS (SELECT 1 FROM party p
                  WHERE p.canonical_name = v.name AND p.role_class = v.rc)"""

SQL_DOMAIN = """
INSERT INTO domain (scheme, name)
SELECT DISTINCT v.scheme, v.name FROM unnest(%s::text[], %s::text[]) AS v(scheme, name)
WHERE NOT EXISTS (SELECT 1 FROM domain d WHERE d.scheme = v.scheme AND d.name = v.name)"""

SQL_CASES = """
INSERT INTO cases (ecli, celex_id, sources, title, date_decision, court_id,
                   language_iso, document_type_id, procedure_type_id,
                   case_number, importance)
SELECT v.ecli, v.celex, ARRAY['CJEU'], v.title, v.date_decision::date, c.id,
       v.lang, dt.id, pt.id, v.case_number, v.importance
FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
            %s::text[], %s::text[], %s::text[], %s::text[], %s::smallint[])
     AS v(ecli, celex, title, date_decision, court_code, lang, doctype, proc,
          case_number, importance)
LEFT JOIN court c ON c.code = v.court_code
LEFT JOIN document_type dt ON dt.code = v.doctype
LEFT JOIN procedure_type pt ON pt.code = v.proc
ON CONFLICT DO NOTHING"""
CASE_FIELDS = ("ecli", "celex", "title", "date_decision", "court_code", "lang",
               "doctype", "proc", "case_number", "importance")

SQL_DOCUMENT = """
INSERT INTO cjeu_document (case_id, celex_id, ecli, sector, case_number,
    formation_id, proc_type, date_lodged, journal_refs, erecueil_ref,
    local_identifier, dossier_uri, citations_extra_info, national_judgement_xml)
SELECT k.id, v.celex, v.ecli, v.sector, v.case_number, f.id, v.proc_type,
       v.date_lodged::date, v.journal_refs, v.erecueil_ref, v.local_identifier,
       v.dossier_uri, v.citations_extra_info, v.national_judgement_xml
FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
            %s::text[], %s::text[], %s::text[], %s::text[], %s::text[],
            %s::text[], %s::text[], %s::text[])
     AS v(ecli, celex, sector, case_number, formation_code, proc_type,
          date_lodged, journal_refs, erecueil_ref, local_identifier,
          dossier_uri, citations_extra_info, national_judgement_xml)
JOIN cases k ON k.ecli = v.ecli
LEFT JOIN court_formation f ON f.code = v.formation_code
ON CONFLICT (case_id) DO NOTHING"""
DOCUMENT_FIELDS = ("ecli", "celex", "sector", "case_number", "formation_code",
                   "proc_type", "date_lodged", "journal_refs", "erecueil_ref",
                   "local_identifier", "dossier_uri", "citations_extra_info",
                   "national_judgement_xml")

SQL_AG = """
INSERT INTO cjeu_ag_opinion (case_id, parent_case_id, advocate_general,
                             opinion_uri, delivered_date)
SELECT k.id, p.id, v.ag, v.opinion_uri, k.date_decision
FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[])
     AS v(ecli, ag, opinion_uri, parent_raw)
JOIN cases k ON k.ecli = v.ecli
LEFT JOIN LATERAL (
    SELECT c2.id FROM cases c2
    WHERE c2.sources @> '{CJEU}'
      AND c2.case_number = replace((regexp_match(coalesce(v.parent_raw, ''),
          '(?:case/)([CTF]-[0-9]+%%2F[0-9]+|[CTF]-[0-9]+/[0-9]+)'))[1], '%%2F', '/')
    ORDER BY c2.id LIMIT 1
) p ON true
ON CONFLICT (case_id) DO NOTHING"""
AG_FIELDS = ("ecli", "ag", "opinion_uri", "parent_raw")

SQL_NATIONAL = f"""
INSERT INTO cjeu_national_document (case_id, {", ".join(NATIONAL_TARGETS)})
SELECT k.id, {", ".join("v." + t for t in NATIONAL_TARGETS)}
FROM unnest({", ".join(["%s::text[]"] * (len(NATIONAL_TARGETS) + 1))})
     AS v(ecli, {", ".join(NATIONAL_TARGETS)})
JOIN cases k ON k.ecli = v.ecli
ON CONFLICT (case_id) DO NOTHING"""
NATIONAL_FIELDS = ("ecli", *NATIONAL_TARGETS)

SQL_CASE_DOMAIN = """
INSERT INTO case_domain (case_id, domain_id)
SELECT DISTINCT k.id, d.id
FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(ecli, scheme, name)
JOIN cases k ON k.ecli = v.ecli
JOIN domain d ON d.scheme = v.scheme AND d.name = v.name
ON CONFLICT (case_id, domain_id) DO NOTHING"""

SQL_CASE_JUDGE = """
INSERT INTO case_judge (case_id, judge_id, role)
SELECT DISTINCT k.id, j.id, v.role
FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(ecli, name, role)
JOIN cases k ON k.ecli = v.ecli
JOIN judge j ON j.full_name = v.name
ON CONFLICT (case_id, judge_id, role) DO NOTHING"""

SQL_CASE_PARTY = """
INSERT INTO case_party (case_id, party_id, role)
SELECT DISTINCT k.id, p.id, v.role
FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[]) AS v(ecli, name, rc, role)
JOIN cases k ON k.ecli = v.ecli
JOIN party p ON p.canonical_name = v.name AND p.role_class = v.rc
ON CONFLICT (case_id, party_id, role, ordinal) DO NOTHING"""

SQL_LAWREF = """
INSERT INTO case_law_reference (case_id, raw_scheme, raw_resource, role, source_dataset)
SELECT DISTINCT k.id, 'celex', v.res, v.role, 'cellar_sparql'
FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(ecli, res, role)
JOIN cases k ON k.ecli = v.ecli
ON CONFLICT DO NOTHING"""

# target resolved via celex (no source filter: RS-anchored targets resolve too);
# target_celex_raw always kept, as in 50
SQL_CITE_OUT = """
INSERT INTO case_citation (source_case_id, target_case_id, target_celex_raw,
    relation_type, source_dataset, is_cross_jurisdiction)
SELECT DISTINCT k.id, t.id, v.target, v.rel, 'cellar_sparql', false
FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(ecli, target, rel)
JOIN cases k ON k.ecli = v.ecli
LEFT JOIN cases t ON t.celex_id = v.target
WHERE t.id IS NULL OR NOT EXISTS (      -- rerun after the target appeared
    SELECT 1 FROM case_citation cc
    WHERE cc.source_case_id = k.id AND cc.target_case_id IS NULL
      AND cc.target_celex_raw = v.target AND cc.relation_type = v.rel
      AND cc.source_dataset = 'cellar_sparql')
ON CONFLICT DO NOTHING"""

# existing case -> new case. Skipped when the same edge is already stored,
# resolved or unresolved (turning an unresolved row resolved is an UPDATE).
SQL_CITE_IN = """
INSERT INTO case_citation (source_case_id, target_case_id, target_celex_raw,
    relation_type, source_dataset, is_cross_jurisdiction)
SELECT DISTINCT s.id, t.id, v.target, v.rel, 'cellar_sparql', false
FROM unnest(%s::text[], %s::text[], %s::text[]) AS v(ecli, target, rel)
JOIN cases s ON s.ecli = v.ecli
JOIN cases t ON t.celex_id = v.target
WHERE NOT EXISTS (
    SELECT 1 FROM case_citation cc
    WHERE cc.source_case_id = s.id AND cc.relation_type = v.rel
      AND cc.source_dataset = 'cellar_sparql'
      AND (cc.target_case_id = t.id
           OR (cc.target_case_id IS NULL AND cc.target_celex_raw = v.target)))
ON CONFLICT DO NOTHING"""


# 50's summary-only row. Skipped if any non-RS text row already exists in that
# language (50 would have UPDATEd the summary onto it; outside this loader's
# remit, and impossible for genuinely new cases).
SQL_SUMMARY = """
INSERT INTO case_text (case_id, language, summary, summary_source, source)
SELECT k.id, v.lang, v.summary, v.summary_source, 'CELLAR_ITEM'
FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[])
     AS v(ecli, lang, summary, summary_source)
JOIN cases k ON k.ecli = v.ecli
WHERE NOT EXISTS (SELECT 1 FROM case_text ct
                  WHERE ct.case_id = k.id AND ct.language = v.lang
                    AND ct.source <> 'RECHTSPRAAK')
ON CONFLICT (case_id, language, source) DO NOTHING"""
SUMMARY_FIELDS = ("ecli", "lang", "summary", "summary_source")

# Linking. Only rows still unresolved; only targets of this batch; never when
# the resolved twin already exists (it would violate case_citation_uk_resolved).
_LINK_TWIN_GUARD = """
   AND NOT EXISTS (SELECT 1 FROM case_citation r
                   WHERE r.source_case_id = cc.source_case_id AND r.target_case_id = t.id
                     AND r.relation_type IS NOT DISTINCT FROM cc.relation_type
                     AND r.source_dataset = cc.source_dataset)"""

SQL_LINK_CELEX = """
UPDATE case_citation cc
   SET target_case_id = t.id
  FROM cases t
 WHERE cc.id = ANY(%s::bigint[])
   AND cc.target_case_id IS NULL
   AND cc.source_dataset = 'cellar_sparql'
   AND t.celex_id = cc.target_celex_raw
   AND t.ecli = ANY(%s::text[])""" + _LINK_TWIN_GUARD

SQL_LINK_ECLI = """
UPDATE case_citation cc
   SET target_case_id = t.id,
       target_ecli_raw = NULL,
       is_cross_jurisdiction = CASE WHEN cc.source_dataset = 'echr_edge' THEN false
                                    ELSE NOT (t.sources @> '{RS}') END
  FROM cases t
 WHERE cc.id = ANY(%s::bigint[])
   AND cc.target_case_id IS NULL
   AND (left(cc.source_dataset, 3) = 'rs_' OR cc.source_dataset = 'echr_edge')
   AND t.ecli = cc.target_ecli_raw
   AND t.ecli = ANY(%s::text[])""" + _LINK_TWIN_GUARD


def columns(rows, fields):
    return [[r[f] for r in rows] for f in fields]


class Writer:
    def __init__(self, runner, row_chunk=1000, log=print):
        self.runner = runner
        self.row_chunk = row_chunk
        self.log = log
        self.inserted = Counter()

    def insert(self, label, sql, rows, to_params, max_rows=None, max_bytes=None,
               size=None):
        """Chunked write (INSERT, or the linking UPDATE); bisects a chunk on
        statement-timeout/row-cap errors."""
        rows = list(rows)
        max_rows = min(max_rows or self.row_chunk, self.row_chunk)
        part, part_bytes = [], 0
        for r in rows:
            b = size(r) if size else 0
            if part and (len(part) >= max_rows or (max_bytes and part_bytes + b > max_bytes)):
                self._insert(label, sql, part, to_params)
                part, part_bytes = [], 0
            part.append(r)
            part_bytes += b
        if part:
            self._insert(label, sql, part, to_params)

    def _insert(self, label, sql, rows, to_params):
        if not rows:
            return
        try:
            out = self.runner.execute(sql, to_params(rows))
        except RunnerError as exc:
            if exc.splittable and len(rows) > 1:
                half = len(rows) // 2
                self.log(f"  {label}: {len(rows)} rows hit {exc.status}; splitting")
                self._insert(label, sql, rows[:half], to_params)
                self._insert(label, sql, rows[half:], to_params)
                return
            raise
        self.inserted[label] += out.get("row_count") or 0


SUMMARY_ROWS = 200            # per statement; summary_tsv is computed server-side
SUMMARY_BYTES = 6_000_000     # same budget 60 uses for text inserts


def execute_plan(runner, plan: Plan, case_batch=400, row_chunk=1000, log=print,
                 summaries=True, link=True, link_chunk=1000):
    w = Writer(runner, row_chunk=row_chunk, log=log)
    new = plan.new

    # A. lookups (insert-only, guarded)
    w.insert("language", SQL_LANGUAGE,
             sorted(_languages_needed(new) if summaries
                    else {m.case["lang"] for m in new if m.case["lang"]}),
             lambda r: [r])
    w.insert("procedure_type", SQL_PROCEDURE,
             sorted({m.case["proc"] for m in new if m.case["proc"]}), lambda r: [r])
    w.insert("judge", SQL_JUDGE, sorted({n for m in new for n, _ in m.judges}), lambda r: [r])
    w.insert("party", SQL_PARTY, sorted({(n, rc) for m in new for n, rc, _ in m.parties}),
             lambda r: [[x[0] for x in r], [x[1] for x in r]])
    w.insert("domain", SQL_DOMAIN, sorted({d for m in new for d in m.domains}),
             lambda r: [[x[0] for x in r], [x[1] for x in r]])

    # B. every cases row first, so citations between new cases resolve
    w.insert("cases", SQL_CASES, [m.case for m in new],
             lambda r: columns(r, CASE_FIELDS))

    # C. satellites per batch; cjeu_document LAST = per-case completion marker
    incoming_by_target = defaultdict(list)
    for e in plan.incoming:
        incoming_by_target[e[1]].append(e)
    links_by_key = defaultdict(list)
    for c in (plan.links if link else ()):
        links_by_key[(c.kind, c.key)].append(c.id)
    for bi, batch in enumerate(chunks(new, case_batch)):
        w.insert("cjeu_ag_opinion", SQL_AG, [m.ag for m in batch if m.ag],
                 lambda r: columns(r, AG_FIELDS))
        w.insert("cjeu_national_document", SQL_NATIONAL,
                 [m.national for m in batch if m.national],
                 lambda r: columns(r, NATIONAL_FIELDS))
        w.insert("case_domain", SQL_CASE_DOMAIN,
                 [(m.ecli, s, n) for m in batch for s, n in sorted(m.domains)], _tuple_cols(3))
        w.insert("case_judge", SQL_CASE_JUDGE,
                 [(m.ecli, n, r) for m in batch for n, r in sorted(m.judges)], _tuple_cols(3))
        w.insert("case_party", SQL_CASE_PARTY,
                 [(m.ecli, n, rc, r) for m in batch for n, rc, r in sorted(m.parties)],
                 _tuple_cols(4))
        w.insert("case_law_reference", SQL_LAWREF,
                 [(m.ecli, t, r) for m in batch for t, r in sorted(m.lawrefs)], _tuple_cols(3))
        w.insert("case_citation (outgoing)", SQL_CITE_OUT,
                 [(m.ecli, t, r) for m in batch for t, r in sorted(m.citations)], _tuple_cols(3))
        w.insert("case_citation (incoming)", SQL_CITE_IN,
                 [e for m in batch for e in incoming_by_target.get(m.celex, ())], _tuple_cols(3))
        if summaries:
            w.insert("case_text (summary)", SQL_SUMMARY, [m.summary for m in batch if m.summary],
                     lambda r: columns(r, SUMMARY_FIELDS), max_rows=SUMMARY_ROWS,
                     max_bytes=SUMMARY_BYTES, size=lambda r: len(r["summary"].encode()))
        if link:
            eclis = [m.ecli for m in batch]
            for kind, sql in (("celex", SQL_LINK_CELEX), ("ecli", SQL_LINK_ECLI)):
                ids = sorted(i for m in batch
                             for i in links_by_key.get((kind, m.celex if kind == "celex" else m.ecli), ()))
                w.insert(f"case_citation link ({kind})", sql, ids,
                         lambda r, e=eclis: [r, e], max_rows=link_chunk)
        w.insert("cjeu_document", SQL_DOCUMENT, [m.document for m in batch],
                 lambda r: columns(r, DOCUMENT_FIELDS))
        log(f"  batch {bi + 1}: {min((bi + 1) * case_batch, len(new))}/{len(new)} cases complete")
    return dict(w.inserted)


def _tuple_cols(n):
    return lambda rows: [[r[i] for r in rows] for i in range(n)]


class ExplainRunner:
    """Dry-run adapter: plans each write statement with EXPLAIN (never
    ANALYZE) through the read-only /query endpoint. Postgres parses, type-
    checks and plans the INSERT against the live schema without executing
    it, so nothing can be written."""

    def __init__(self, runner):
        self.runner = runner
        self.statements = 0

    def execute(self, sql, params=None):
        self.runner.query("EXPLAIN (COSTS OFF) " + sql, params)
        self.statements += 1
        return {"row_count": 0}


def explain_plan(runner, plan: Plan, n=25, log=print):
    """Validate every write statement with real parameters from the first
    ``n`` planned cases. Read-only."""
    sub_new = list(plan.new[:n])
    # make sure both link statements get planned too
    for kind in ("celex", "ecli"):
        keys = {c.key for c in plan.links if c.kind == kind}
        extra = [m for m in plan.new if (m.celex if kind == "celex" else m.ecli) in keys]
        sub_new += [m for m in extra[:3] if m not in sub_new]
    targets = {m.celex for m in sub_new}
    target_keys = targets | {m.ecli for m in sub_new}
    sub = Plan(new=sub_new, attach_existing=plan.attach_existing,
               incoming=[e for e in plan.incoming if e[1] in targets],
               errors=[], warnings=[], lookups={}, stats={},
               links=[c for c in plan.links if c.key in target_keys])
    ex = ExplainRunner(runner)
    execute_plan(ex, sub, case_batch=max(len(sub_new), 1), log=lambda *_: None)
    log(f"EXPLAIN-validated {ex.statements} write statements against the live schema "
        f"({len(sub_new)} cases, {len(sub.incoming)} incoming edges, {len(sub.links)} "
        f"link candidates); nothing executed")
    return ex.statements


# ---------------------------------------------------------------------------
# Parity check (read-only): does map_row reproduce live rows of existing cases?
# ---------------------------------------------------------------------------

PARITY_SQL = {
    "cases": """SELECT c.ecli, c.celex_id, c.title, c.date_decision::text AS date_decision,
                  co.code AS court_code, c.language_iso, dt.code AS doctype,
                  pt.code AS proc, c.case_number, c.importance
                FROM cases c LEFT JOIN court co ON co.id = c.court_id
                LEFT JOIN document_type dt ON dt.id = c.document_type_id
                LEFT JOIN procedure_type pt ON pt.id = c.procedure_type_id
                WHERE c.ecli = ANY(%s::text[])""",
    "cjeu_document": """SELECT d.ecli, d.celex_id, d.sector, d.case_number, f.code AS formation_code,
                  d.proc_type, d.date_lodged::text AS date_lodged, d.journal_refs,
                  d.erecueil_ref, d.local_identifier, d.dossier_uri,
                  md5(coalesce(d.citations_extra_info,'')) AS cei_md5,
                  md5(coalesce(d.national_judgement_xml,'')) AS njx_md5
                FROM cjeu_document d LEFT JOIN court_formation f ON f.id = d.formation_id
                WHERE d.ecli = ANY(%s::text[])""",
    "domains": """SELECT c.ecli, d.scheme, d.name FROM case_domain cd
                JOIN cases c ON c.id = cd.case_id JOIN domain d ON d.id = cd.domain_id
                WHERE c.ecli = ANY(%s::text[]) AND d.scheme IN
                ('cjeu_subject_matter','eurovoc','cjeu_keyword','cjeu_directory_code','cjeu_is_about_subject')""",
    "judges": """SELECT c.ecli, j.full_name, cj.role FROM case_judge cj
                JOIN cases c ON c.id = cj.case_id JOIN judge j ON j.id = cj.judge_id
                WHERE c.ecli = ANY(%s::text[])""",
    "parties": """SELECT c.ecli, p.canonical_name, p.role_class, cp.role FROM case_party cp
                JOIN cases c ON c.id = cp.case_id JOIN party p ON p.id = cp.party_id
                WHERE c.ecli = ANY(%s::text[])""",
    "lawrefs": """SELECT c.ecli, r.raw_resource, r.role FROM case_law_reference r
                JOIN cases c ON c.id = r.case_id
                WHERE c.ecli = ANY(%s::text[]) AND r.source_dataset = 'cellar_sparql'""",
    "citations": """SELECT c.ecli, coalesce(cc.target_celex_raw, t.celex_id) AS target, cc.relation_type
                FROM case_citation cc JOIN cases c ON c.id = cc.source_case_id
                LEFT JOIN cases t ON t.id = cc.target_case_id
                WHERE c.ecli = ANY(%s::text[]) AND cc.source_dataset = 'cellar_sparql'""",
    "ag": """SELECT c.ecli, a.advocate_general, a.opinion_uri FROM cjeu_ag_opinion a
                JOIN cases c ON c.id = a.case_id WHERE c.ecli = ANY(%s::text[])""",
    "national": """SELECT c.ecli, n.* FROM cjeu_national_document n
                JOIN cases c ON c.id = n.case_id WHERE c.ecli = ANY(%s::text[])""",
    "summaries": """SELECT DISTINCT c.ecli, ct.language, md5(ct.summary) AS smd5, ct.summary_source
                FROM case_text ct JOIN cases c ON c.id = ct.case_id
                WHERE c.ecli = ANY(%s::text[]) AND ct.summary IS NOT NULL
                  AND ct.source <> 'RECHTSPRAAK'""",
}


def _md5(v):
    return hashlib.md5((v or "").encode()).hexdigest()


def expected_sets(m: CaseMapping):
    c, d = m.case, m.document
    return {
        "cases": {(m.ecli, c["celex"], c["title"], c["date_decision"], c["court_code"],
                   c["lang"], c["doctype"], c["proc"], c["case_number"], c["importance"])},
        "cjeu_document": {(m.ecli, d["celex"], d["sector"], d["case_number"], d["formation_code"],
                           d["proc_type"], d["date_lodged"], d["journal_refs"], d["erecueil_ref"],
                           d["local_identifier"], d["dossier_uri"],
                           _md5(d["citations_extra_info"]), _md5(d["national_judgement_xml"]))},
        "domains": {(m.ecli, s, n) for s, n in m.domains},
        "judges": {(m.ecli, n, r) for n, r in m.judges},
        "parties": {(m.ecli, n, rc, r) for n, rc, r in m.parties},
        "lawrefs": {(m.ecli, t, r) for t, r in m.lawrefs},
        "citations": {(m.ecli, t, r) for t, r in m.citations},
        "ag": {(m.ecli, m.ag["ag"], m.ag["opinion_uri"])} if m.ag else set(),
        "national": {(m.ecli, *[m.national[t] for t in NATIONAL_TARGETS])} if m.national else set(),
        "summaries": {(m.ecli, m.summary["lang"], _md5(m.summary["summary"]),
                       m.summary["summary_source"])} if m.summary else set(),
    }


def _live_tuple(table, r):
    if table == "cases":
        return (r["ecli"], r["celex_id"], r["title"], r["date_decision"], r["court_code"],
                r["language_iso"], r["doctype"], r["proc"], r["case_number"], r["importance"])
    if table == "cjeu_document":
        return (r["ecli"], r["celex_id"], r["sector"], r["case_number"], r["formation_code"],
                r["proc_type"], r["date_lodged"], r["journal_refs"], r["erecueil_ref"],
                r["local_identifier"], r["dossier_uri"], r["cei_md5"], r["njx_md5"])
    if table == "domains":
        return (r["ecli"], r["scheme"], r["name"])
    if table == "judges":
        return (r["ecli"], r["full_name"], r["role"])
    if table == "parties":
        return (r["ecli"], r["canonical_name"], r["role_class"], r["role"])
    if table == "lawrefs":
        return (r["ecli"], r["raw_resource"], r["role"])
    if table == "citations":
        return (r["ecli"], r["target"], r["relation_type"])
    if table == "ag":
        return (r["ecli"], r["advocate_general"], r["opinion_uri"])
    if table == "national":
        return (r["ecli"], *[r[t] for t in NATIONAL_TARGETS])
    if table == "summaries":
        return (r["ecli"], r["language"], r["smd5"], r["summary_source"])
    raise KeyError(table)


def parity_check(runner, df, prod: ProdState, n, seed=62, include_keywords=False):
    existing = df[df["x_ecli"].isin(prod.cjeu_eclis.keys())]
    sample = existing.sample(n=min(n, len(existing)), random_state=seed)
    maps = [map_row(r, include_keywords) for r in sample.to_dict("records")]
    report = {}
    for table, sql in PARITY_SQL.items():
        exp, live = set(), set()
        for m in maps:
            exp |= expected_sets(m)[table]
        for part in chunks([m.ecli for m in maps], 5):
            res = runner.query(sql, [part])
            if res.get("truncated"):
                raise RuntimeError(f"parity read truncated for {table}")
            live |= {_live_tuple(table, r) for r in res["rows"]}
        report[table] = {"expected": len(exp), "live": len(live),
                         "missing_live": sorted(exp - live, key=str)[:5],
                         "extra_live": sorted(live - exp, key=str)[:5],
                         "n_missing_live": len(exp - live), "n_extra_live": len(live - exp)}
    return report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def print_plan(plan: Plan, resolution, log=print):
    log("\n=== PLAN ===")
    for k, v in plan.stats.items():
        log(f"  {k}: {v}")
    log("\nrows to insert per table:")
    for k, v in planned_counts(plan).items():
        log(f"  {k:48s} {v:>8,}")
    log("\ncitation resolution:")
    for k, v in resolution.items():
        log(f"  {k}: {v}")
    log("\nexisting unresolved case_citation rows to link (UPDATE target_case_id):")
    log(f"  total: {len(plan.links):,} {link_counts(plan)}")
    log(f"  not linkable (no resolution rule; reported only): {dict(plan.unlinkable)}")
    log("\nlookup rows to create / unmapped values:")
    for k, v in plan.lookups.items():
        log(f"  {k}: {len(v)} {v[:8]}")
    if plan.attach_existing:
        log(f"\nexisting cases rows gaining a CJEU satellite: {len(plan.attach_existing)} "
            f"{sorted(plan.attach_existing)[:8]}")
    log(f"\nvalidation errors (rows excluded): {len(plan.errors)}")
    kinds = Counter("celex owned by another production case" if "already belongs" in msg
                    else "celex shared by several new corpus rows" if "shared with" in msg
                    else "malformed celex" if "malformed" in msg
                    else "other" for _, msg in plan.errors)
    for k, v in kinds.items():
        log(f"  {k}: {v}")
    for e, msg in plan.errors[:10]:
        log(f"  {e}: {msg}")
    if plan.errors and os.environ.get("ERRORS_TSV"):
        with open(os.environ["ERRORS_TSV"], "w", encoding="utf-8") as fh:
            fh.write("ecli\terror\n")
            fh.writelines(f"{e}\t{msg}\n" for e, msg in plan.errors)
        log(f"  full list written to {os.environ['ERRORS_TSV']}")
    log(f"warnings: {len(plan.warnings)}")
    for w in plan.warnings[:20]:
        log(f"  {w}")
    kinds = Counter((m.document["sector"], m.case["doctype"], m.case["court_code"]) for m in plan.new)
    log("\nnew cases by (sector, document_type, court):")
    for k, v in kinds.most_common():
        log(f"  {k}: {v:,}")
    log("\nsample rows:")
    for m in plan.new[:int(os.environ.get("SAMPLE_ROWS", "3"))]:
        log(json.dumps({"case": m.case, "cjeu_document": {**m.document,
            "citations_extra_info": (m.document["citations_extra_info"] or "")[:80] or None,
            "national_judgement_xml": (m.document["national_judgement_xml"] or "")[:80] or None},
            "ag": m.ag, "national": m.national,
            "domains": sorted(m.domains)[:5], "judges": sorted(m.judges)[:5],
            "parties": sorted(m.parties)[:5], "lawrefs": sorted(m.lawrefs)[:5],
            "citations": sorted(m.citations)[:5]}, ensure_ascii=False, indent=1))


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "1") != "0"
    runner = Runner(os.environ["SQL_RUNNER_URL"], os.environ["SQL_RUNNER_TOKEN"],
                    os.environ.get("SQL_RUNNER_CONFIRM", "execute-cle-v2"), dry_run=dry_run)
    path = os.environ.get("CASES_PARQUET")
    if not path:
        from huggingface_hub import hf_hub_download
        path = hf_hub_download(HF_REPO, "cases.parquet", repo_type="dataset")
    extra = load_extra_citations(os.environ["CITATIONS_PARQUET"]) \
        if os.environ.get("CITATIONS_PARQUET") else None
    limit = int(os.environ["LIMIT_NEW"]) if os.environ.get("LIMIT_NEW") else None
    include_keywords = os.environ.get("INCLUDE_CJEU_KEYWORD") == "1"

    print(f"mode: {'DRY RUN (read-only)' if dry_run else 'WRITE'}", flush=True)
    df = load_corpus(path)
    print(f"corpus: {len(df):,} cases from {path}", flush=True)
    prod = read_prod_state(runner)
    print(f"production: {len(prod.cjeu_eclis):,} CJEU cases", flush=True)
    summaries = os.environ.get("SKIP_SUMMARIES") != "1"
    link = os.environ.get("SKIP_CITATION_LINKING") != "1"
    plan = build_plan(df, prod, runner, extra_edges=extra, limit=limit,
                      include_keywords=include_keywords)
    if not summaries:
        for m in plan.new:
            m.summary = None
    if link:
        collect_link_candidates(runner, plan)
    resolution = citation_resolution(runner, plan)
    print_plan(plan, resolution)

    if dry_run and os.environ.get("PARITY_SAMPLE"):
        n = int(os.environ["PARITY_SAMPLE"])
        print(f"\n=== PARITY: mapping vs live rows for {n} existing CJEU cases ===")
        for table, r in parity_check(runner, df, prod, n,
                                     include_keywords=include_keywords).items():
            print(f"  {table}: {json.dumps(r, ensure_ascii=False, default=str)}")

    if dry_run and os.environ.get("EXPLAIN_SQL") == "1" and plan.new:
        print("\n=== EXPLAIN (no execution) of the write statements ===")
        explain_plan(runner, plan)

    if dry_run:
        print(f"\nDRY RUN: {runner.calls['query']} /query calls, "
              f"{runner.calls['execute']} /execute calls. Nothing written.")
        return 0
    if plan.errors and os.environ.get("SKIP_INVALID") != "1":
        print(f"refusing to write: {len(plan.errors)} validation errors (set SKIP_INVALID=1 "
              f"to load the valid rows and skip these)")
        return 2
    if not plan.new:
        print("nothing to load")
        return 0
    inserted = execute_plan(runner, plan,
                            case_batch=int(os.environ.get("CASE_BATCH", "400")),
                            row_chunk=int(os.environ.get("ROW_CHUNK", "1000")),
                            summaries=summaries, link=link,
                            link_chunk=int(os.environ.get("LINK_CHUNK", "1000")))
    print("rows written (inserted, or linked by UPDATE):")
    for k, v in inserted.items():
        print(f"  {k:32s} {v:>8,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
