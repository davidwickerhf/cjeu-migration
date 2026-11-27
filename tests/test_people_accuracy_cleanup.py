"""Unit tests for P0 lower-court removal and P1 Art. 5(1)(d) people accuracy cleanup."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from migration import backfill_cross_court_judges as backfill  # noqa: E402
from migration import cleanup_people_accuracy as cleanup  # noqa: E402


# ---------------------------------------------------------------------------
# P0: backfill_cross_court_judges.py lower-court & French-court guards
# ---------------------------------------------------------------------------

def test_backfill_corpus_specs_exclude_rs_all():
    assert set(backfill._CORPUS_SPECS.keys()) == {"echr", "rs-phr", "rs-hr"}
    assert "rs-all" not in backfill._CORPUS_SPECS


def test_backfill_cli_help_works_without_cle_checkout():
    env = {k: v for k, v in os.environ.items() if k not in {"CLE_CHECKOUT", "SQL_RUNNER_TOKEN"}}
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migration" / "backfill_cross_court_judges.py"), "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert "rs-all" not in proc.stdout
    for allowed in ("all", "echr", "rs-phr", "rs-hr"):
        assert allowed in proc.stdout


def test_backfill_cli_rejects_rs_all():
    env = {k: v for k, v in os.environ.items() if k not in {"CLE_CHECKOUT", "SQL_RUNNER_TOKEN"}}
    proc = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "migration" / "backfill_cross_court_judges.py"),
            "--corpus",
            "rs-all",
        ],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode != 0
    assert "invalid choice: 'rs-all'" in proc.stderr


@pytest.mark.parametrize(
    "ecli,corpus",
    [
        ("ECLI:NL:HR:2023:1234", "rs-hr"),
        ("ECLI:NL:PHR:2023:567", "rs-phr"),
        ("ECLI:CE:ECHR:2020:0101JUD0001234", "echr"),
        ("ECLI:EU:C:2020:100", None),
        ("", "echr"),
    ],
)
def test_validate_ecli_allows_supreme_and_international_courts(ecli: str, corpus: Optional[str]):
    backfill.validate_ecli_allowed(ecli, corpus=corpus)


@pytest.mark.parametrize(
    "ecli",
    [
        "ECLI:NL:RBAMS:2023:100",
        "ECLI:NL:GHAMS:2022:55",
        "ECLI:NL:RVS:2021:99",
        "ECLI:NL:CRVB:2020:12",
        "ECLI:NL:CBB:2020:7",
        "ecli:nl:rbrot:2023:1",
    ],
)
def test_validate_ecli_refuses_lower_dutch_courts(ecli: str):
    with pytest.raises(ValueError, match="lower Dutch court"):
        backfill.validate_ecli_allowed(ecli)


@pytest.mark.parametrize(
    "ecli",
    [
        "ECLI:FR:CCASS:2022:C100123",
        "ECLI:FR:CECHR:2021:444444",
        "ecli:fr:ca:2020:1",
    ],
)
def test_validate_ecli_refuses_french_courts(ecli: str):
    with pytest.raises(ValueError, match="Art. 33"):
        backfill.validate_ecli_allowed(ecli)


def test_validate_ecli_refuses_corpus_prefix_mismatch():
    with pytest.raises(ValueError, match="expected prefix"):
        backfill.validate_ecli_allowed("ECLI:NL:PHR:2023:1", corpus="rs-hr")
    with pytest.raises(ValueError, match="expected prefix"):
        backfill.validate_ecli_allowed("ECLI:NL:HR:2023:1", corpus="rs-phr")


def test_run_corpus_rejects_rs_all_and_blocks_prohibited_ecli_before_extraction():
    class StubClient:
        def __init__(self, ecli_to_return: str) -> None:
            self.ecli_to_return = ecli_to_return
            self.executed: List[str] = []

        def query(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
            if "FROM cle_v2.court" in sql:
                return [{"id": 439}]
            if "FROM pool p" in sql:
                return [{"id": 1001, "ecli": self.ecli_to_return}]
            return []

        def execute(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
            self.executed.append(sql)
            return {"row_count": 0}

    extract_called = False

    def fail_extract(*_args: Any, **_kwargs: Any):
        nonlocal extract_called
        extract_called = True
        return []

    judges_mod = SimpleNamespace(extract_echr_judges=fail_extract, extract_rs_judges=fail_extract)

    with pytest.raises(ValueError, match="Unsupported corpus"):
        backfill.run_corpus(
            StubClient("ECLI:NL:HR:2023:1"),
            judges_mod,
            "rs-all",
            batch_cases=10,
            start_id=None,
            newest_first=False,
            limit_cases=10,
            sample_limit=5,
            apply=False,
        )

    for forbidden in ("ECLI:NL:RBAMS:2023:1", "ECLI:FR:CCASS:2022:1"):
        client = StubClient(forbidden)
        with pytest.raises(ValueError):
            backfill.run_corpus(
                client,
                judges_mod,
                "rs-hr",
                batch_cases=10,
                start_id=None,
                newest_first=False,
                limit_cases=10,
                sample_limit=5,
                apply=False,
            )
        assert not extract_called
        assert client.executed == []


# ---------------------------------------------------------------------------
# P1: cleanup_people_accuracy.py normalization, splitting, and CLI tests
# ---------------------------------------------------------------------------

def test_cleanup_cli_help_works_without_token():
    env = {k: v for k, v in os.environ.items() if k != "SQL_RUNNER_TOKEN"}
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "migration" / "cleanup_people_accuracy.py"), "--help"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0
    assert "--apply" in proc.stdout
    assert "--url" in proc.stdout


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("AlberAlber", "Alber"),
        ("Campos Sánchez-BordonaCampos Sánchez-Bordona", "Campos Sánchez-Bordona"),
        ("CosmasCosmas", "Cosmas"),
        ("EmiliouEmiliou", "Emiliou"),
        ("GeelhoedGeelhoed", "Geelhoed"),
        ("JääskinenJääskinen", "Jääskinen"),
        ("La PergolaLa Pergola", "La Pergola"),
        ("LégerLéger", "Léger"),
        ("MazákMazák", "Mazák"),
        ("MengozziMengozzi", "Mengozzi"),
        ("MischoMischo", "Mischo"),
        ("Richard de la TourRichard de la Tour", "Richard de la Tour"),
        ("Ruiz-Jarabo ColomerRuiz-Jarabo Colomer", "Ruiz-Jarabo Colomer"),
        ("Stix-HacklStix-Hackl", "Stix-Hackl"),
        ("SzpunarSzpunar", "Szpunar"),
        ("TrstenjakTrstenjak", "Trstenjak"),
    ],
)
def test_strip_self_concatenation_handles_all_empirical_doubles(raw: str, expected: str):
    assert cleanup.strip_self_concatenation(raw) == expected
    target, category, _basis = cleanup.normalize_ag_opinion_value(raw)
    assert target == expected
    assert category == "self_concatenation"


@pytest.mark.parametrize(
    "clean",
    ["Bot", "Alber", "Kokott", "Szpunar", "Ćapeta", "La Pergola", "Stix-Hackl", "Van Gerven"],
)
def test_normalize_ag_opinion_leaves_clean_names_untouched(clean: str):
    assert cleanup.strip_self_concatenation(clean) is None
    assert cleanup.normalize_ag_opinion_value(clean) == (None, None, None)


def test_normalize_ag_opinion_diacritic_and_two_person_fusions():
    assert cleanup.normalize_ag_opinion_value("Capeta")[0:2] == ("Ćapeta", "diacritic")

    expected_fusions = {
        ("BotLéger", "ECLI:EU:C:2006:646"): "Léger",
        ("BotLéger", "ECLI:EU:C:2008:386"): "Bot",
        ("SharpstonJacobs", "ECLI:EU:C:2005:656"): "Jacobs",
        ("SharpstonJacobs", "ECLI:EU:C:2006:748"): "Sharpston",
        ("Stix-HacklJacobs", "ECLI:EU:C:2005:183"): "Jacobs",
        ("Stix-HacklJacobs", "ECLI:EU:C:2006:172"): "Stix-Hackl",
        ("Stix-HacklTizzano", "ECLI:EU:C:2005:676"): "Tizzano",
        ("Stix-HacklTizzano", "ECLI:EU:C:2006:642"): "Stix-Hackl",
        ("TizzanoMengozzi", "ECLI:EU:C:2006:213"): "Tizzano",
        ("TizzanoMengozzi", "ECLI:EU:C:2006:747"): "Mengozzi",
        ("TrstenjakGeelhoed", "ECLI:EU:C:2005:390"): "Geelhoed",
        ("TrstenjakGeelhoed", "ECLI:EU:C:2007:145"): "Trstenjak",
    }
    for (fused, ecli), expected in expected_fusions.items():
        target, category, basis = cleanup.normalize_ag_opinion_value(fused, ecli=ecli)
        assert target == expected
        assert category == "two_person_fusion"
        assert basis

    # Header fallback when ECLI is not in the static table
    target, category, basis = cleanup.normalize_ag_opinion_value(
        "BotLéger",
        ecli="ECLI:EU:C:2099:999",
        text_headers=["OPINION OF ADVOCATE GENERAL YVES BOT delivered on 8 July 2008"],
    )
    assert target == "Bot"
    assert category == "two_person_fusion"
    assert basis == "unambiguous_opinion_header_match"

    # Ambiguous fallback never guesses
    target, category, _basis = cleanup.normalize_ag_opinion_value(
        "BotLéger",
        ecli="ECLI:EU:C:2099:999",
        text_headers=["Opinion mentioning both Bot and Léger"],
    )
    assert target is None
    assert category == "unresolved_fusion"


@pytest.mark.parametrize(
    "src,court,date_decision,ecli,expected",
    [
        ("Rossi", "CJEU", "1960-02-11", "ECLI:EU:C:1960:12", "Rino Rossi"),
        ("Rossi", "CJEU", "1964-07-07", "ECLI:EU:C:1964:23", "Rino Rossi"),
        ("Rossi", "CJEU", "2018-10-05", "ECLI:EU:C:2018:825", "Lucia Serena Rossi"),
        ("Rossi", "CJEU", "2024-10-04", "ECLI:EU:C:2024:130", "Lucia Serena Rossi"),
        ("Rossi", "CJEU", "1995-05-01", "ECLI:EU:C:1995:1", None),
        ("Rossi", "EGC", "2020-05-01", "ECLI:EU:T:2020:1", None),
        ("O'Higgins", "CJEU", "1985-05-08", "ECLI:EU:C:1985:177", "Thomas Francis O'Higgins"),
        ("O'Higgins", "CJEU", "1991-07-25", "ECLI:EU:C:1991:116", "Thomas Francis O'Higgins"),
        ("O'Higgins", "EGC", "2006-10-27", "ECLI:EU:T:2006:338", "Kevin O'Higgins"),
        ("O'Higgins", "EGC", "2014-10-02", "ECLI:EU:T:2014:847", "Kevin O'Higgins"),
        ("O\u2019Higgins", "EGC", "2011-02-03", "ECLI:EU:T:2011:133", "Kevin O'Higgins"),
        ("O'Higgins", "CJEU", "2011-02-03", "ECLI:EU:C:2011:1", None),
        ("O'Higgins", "EGC", "1988-02-03", "ECLI:EU:T:1988:1", None),
    ],
)
def test_classify_homonym_case(
    src: str, court: str, date_decision: str, ecli: str, expected: Optional[str]
):
    assert cleanup.classify_homonym_case(src, court, date_decision, ecli) == expected


def test_merge_alias_list_deduplicates_and_excludes_canonical():
    result = cleanup.merge_alias_list(
        "D.J.M.W. Paridaens",
        ["D.J.M.W. Paridaens", "D.J. M .W. Paridaens", ""],
        ["D.J. M .W. Paridaens", "Paridaens"],
    )
    assert result == ["D.J. M .W. Paridaens", "Paridaens"]


class _FakeDbClient:
    """Minimal in-memory SQL client exercising dry-run, --apply, and idempotency."""

    def __init__(self) -> None:
        self.ag_opinions: Dict[int, Dict[str, Any]] = {
            1: {"id": 1, "case_id": 10, "advocate_general": "SzpunarSzpunar", "ecli": "ECLI:EU:C:2020:1", "celex_id": "62020CC0001"},
            2: {"id": 2, "case_id": 11, "advocate_general": "Capeta", "ecli": "ECLI:EU:C:2023:2", "celex_id": "62023CC0002"},
            3: {"id": 3, "case_id": 12, "advocate_general": "BotLéger", "ecli": "ECLI:EU:C:2006:646", "celex_id": "62005CC0110"},
            4: {"id": 4, "case_id": 13, "advocate_general": "BotLéger", "ecli": "ECLI:EU:C:2008:386", "celex_id": "62005CC0110(01)"},
        }
        self.judges: Dict[int, Dict[str, Any]] = {
            504: {"id": 504, "full_name": "D.J.M.W. Paridaens", "aliases": None},
            529: {"id": 529, "full_name": "D.J. M .W. Paridaens", "aliases": None},
            440: {"id": 440, "full_name": "van der Woude", "aliases": None},
            418: {"id": 418, "full_name": "M. Van der Woude", "aliases": None},
            142: {"id": 142, "full_name": "Rossi", "aliases": None},
            116: {"id": 116, "full_name": "O'Higgins", "aliases": None},
            397: {"id": 397, "full_name": "O\u2019Higgins", "aliases": None},
        }
        self.next_judge_id = 1000
        self.next_cj_id = 5000
        self.case_judges: List[Dict[str, Any]] = [
            # PHR merge (non-conflicting)
            {"id": 100, "case_id": 201, "judge_id": 529, "role": "advocate_general", "source_dataset": "rs_phr", "extractor_version": "v1", "confidence": 0.9},
            # Diacritic/case merge (conflicting with existing canonical row!)
            {"id": 101, "case_id": 202, "judge_id": 440, "role": "judge", "source_dataset": None, "extractor_version": None, "confidence": None},
            {"id": 102, "case_id": 202, "judge_id": 418, "role": "judge", "source_dataset": None, "extractor_version": None, "confidence": None},
            # Rossi homonyms
            {"id": 103, "case_id": 301, "judge_id": 142, "role": "judge", "source_dataset": None, "extractor_version": None, "confidence": None},
            {"id": 104, "case_id": 302, "judge_id": 142, "role": "rapporteur", "source_dataset": None, "extractor_version": None, "confidence": None},
            # O'Higgins homonyms (both straight and curly apostrophe)
            {"id": 105, "case_id": 401, "judge_id": 116, "role": "judge", "source_dataset": None, "extractor_version": None, "confidence": None},
            {"id": 106, "case_id": 402, "judge_id": 116, "role": "rapporteur", "source_dataset": None, "extractor_version": None, "confidence": None},
            {"id": 107, "case_id": 403, "judge_id": 397, "role": "judge", "source_dataset": None, "extractor_version": None, "confidence": None},
        ]
        self.cases: Dict[int, Dict[str, Any]] = {
            301: {"ecli": "ECLI:EU:C:1961:14", "date_decision": "1961-06-30", "court_code": "CJEU"},
            302: {"ecli": "ECLI:EU:C:2022:1014", "date_decision": "2022-05-12", "court_code": "CJEU"},
            401: {"ecli": "ECLI:EU:C:1988:104", "date_decision": "1988-03-10", "court_code": "CJEU"},
            402: {"ecli": "ECLI:EU:T:2012:124", "date_decision": "2012-03-15", "court_code": "EGC"},
            403: {"ecli": "ECLI:EU:T:2011:133", "date_decision": "2011-02-03", "court_code": "EGC"},
        }
        self.execute_calls: List[str] = []

    def query(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        params = params or []
        if "information_schema.columns" in sql:
            return [{"column_name": c} for c in ("source_dataset", "extractor_version", "confidence")]
        if "GROUP BY advocate_general" in sql:
            counts: Dict[str, int] = {}
            for r in self.ag_opinions.values():
                counts[r["advocate_general"]] = counts.get(r["advocate_general"], 0) + 1
            return [{"advocate_general": k, "cnt": v} for k, v in sorted(counts.items())]
        if "FROM cle_v2.cjeu_ag_opinion o" in sql:
            wanted = set(params[0])
            return [dict(r) for r in self.ag_opinions.values() if r["advocate_general"] in wanted]
        if "COUNT(cj.id) AS link_cnt" in sql:
            wanted = set(params[0])
            out = []
            for j in self.judges.values():
                if j["full_name"] in wanted:
                    cnt = sum(1 for cj in self.case_judges if cj["judge_id"] == j["id"])
                    out.append({**j, "link_cnt": cnt})
            return out
        if "FROM cle_v2.case_judge a" in sql:
            old_id, canon_id = params
            old_pairs = {(cj["case_id"], cj["role"]) for cj in self.case_judges if cj["judge_id"] == old_id}
            canon_pairs = {(cj["case_id"], cj["role"]) for cj in self.case_judges if cj["judge_id"] == canon_id}
            return [{"cnt": len(old_pairs & canon_pairs)}]
        if "FROM cle_v2.judge WHERE full_name = ANY" in sql:
            wanted = set(params[0])
            return [dict(j) for j in self.judges.values() if j["full_name"] in wanted]
        if "WHERE lower(full_name) = lower(%s)" in sql:
            target_l = params[0].lower()
            return [dict(j) for j in self.judges.values() if j["full_name"].lower() == target_l]
        if "SELECT cj.id AS cj_id" in sql:
            src_id = params[0]
            out = []
            for cj in self.case_judges:
                if cj["judge_id"] == src_id:
                    meta = self.cases[cj["case_id"]]
                    out.append({
                        "cj_id": cj["id"],
                        "case_id": cj["case_id"],
                        "role": cj["role"],
                        "source_dataset": cj["source_dataset"],
                        "extractor_version": cj["extractor_version"],
                        "confidence": cj["confidence"],
                        **meta,
                    })
            return out
        raise AssertionError(f"Unhandled query SQL: {sql}")

    def execute(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        params = params or []
        self.execute_calls.append(sql)
        if "UPDATE cle_v2.cjeu_ag_opinion" in sql:
            batch = json.loads(params[0])
            updated = 0
            for item in batch:
                row = self.ag_opinions.get(item["id"])
                if row and row["advocate_general"] == item["old_ag"]:
                    row["advocate_general"] = item["new_ag"]
                    updated += 1
            return {"row_count": updated}
        if "INSERT INTO cle_v2.case_judge" in sql and "FROM cle_v2.case_judge cj" in sql:
            canon_id, old_id = params
            existing = {(cj["case_id"], cj["judge_id"], cj["role"]) for cj in self.case_judges}
            inserted = 0
            for cj in list(self.case_judges):
                if cj["judge_id"] == old_id:
                    key = (cj["case_id"], canon_id, cj["role"])
                    if key not in existing:
                        self.next_cj_id += 1
                        self.case_judges.append({**cj, "id": self.next_cj_id, "judge_id": canon_id})
                        existing.add(key)
                        inserted += 1
            return {"row_count": inserted}
        if "INSERT INTO cle_v2.case_judge" in sql and "jsonb_array_elements" in sql:
            batch = json.loads(params[0])
            existing = {(cj["case_id"], cj["judge_id"], cj["role"]) for cj in self.case_judges}
            inserted = 0
            for item in batch:
                key = (item["case_id"], item["judge_id"], item["role"])
                if key not in existing:
                    self.next_cj_id += 1
                    self.case_judges.append({**item, "id": self.next_cj_id})
                    existing.add(key)
                    inserted += 1
            return {"row_count": inserted}
        if "UPDATE cle_v2.judge" in sql and "SET aliases" in sql:
            add_aliases, jid = params
            j = self.judges[jid]
            j["aliases"] = cleanup.merge_alias_list(j["full_name"], j.get("aliases"), add_aliases)
            return {"row_count": 1}
        if "DELETE FROM cle_v2.case_judge" in sql and "ctid IN" in sql:
            jid, limit = params
            to_remove = [cj["id"] for cj in self.case_judges if cj["judge_id"] == jid][:limit]
            self.case_judges = [cj for cj in self.case_judges if cj["id"] not in set(to_remove)]
            return {"row_count": len(to_remove)}
        if "DELETE FROM cle_v2.case_judge WHERE id = ANY" in sql:
            ids = set(params[0])
            before = len(self.case_judges)
            self.case_judges = [cj for cj in self.case_judges if cj["id"] not in ids]
            return {"row_count": before - len(self.case_judges)}
        if "DELETE FROM cle_v2.judge" in sql:
            jid = params[0]
            has_links = any(cj["judge_id"] == jid for cj in self.case_judges)
            if not has_links and jid in self.judges:
                del self.judges[jid]
                return {"row_count": 1}
            return {"row_count": 0}
        if "INSERT INTO cle_v2.judge (full_name)" in sql:
            full_name = params[0]
            if not any(j["full_name"].lower() == full_name.lower() for j in self.judges.values()):
                self.next_judge_id += 1
                self.judges[self.next_judge_id] = {"id": self.next_judge_id, "full_name": full_name, "aliases": None}
                return {"row_count": 1}
            return {"row_count": 0}
        raise AssertionError(f"Unhandled execute SQL: {sql}")


def test_run_cleanup_dry_run_apply_and_idempotency():
    db = _FakeDbClient()

    # 1. Dry-run executes ZERO write SQL statements
    dry = cleanup.run_cleanup(db, apply=False)
    assert dry["mode"] == "DRY-RUN"
    assert db.execute_calls == []
    assert dry["cjeu_ag_opinion_cleanup"]["planned_updates"] == 4
    assert dry["judge_duplicate_merges"]["summary"]["planned_merges"] == 2
    assert dry["judge_duplicate_merges"]["summary"]["planned_conflicts_deduped"] == 1
    assert dry["judge_homonym_splits"]["summary"]["planned_sources_to_split"] == 3
    assert dry["judge_homonym_splits"]["summary"]["planned_classified_rows"] == 5
    assert dry["judge_homonym_splits"]["summary"]["planned_unclassified_rows"] == 0

    # 2. --apply mutates state cleanly
    applied = cleanup.run_cleanup(db, apply=True)
    assert applied["mode"] == "APPLIED"
    assert applied["cjeu_ag_opinion_cleanup"]["applied_updates"] == 4
    assert db.ag_opinions[1]["advocate_general"] == "Szpunar"
    assert db.ag_opinions[2]["advocate_general"] == "Ćapeta"
    assert db.ag_opinions[3]["advocate_general"] == "Léger"
    assert db.ag_opinions[4]["advocate_general"] == "Bot"

    assert 529 not in db.judges
    assert db.judges[504]["aliases"] == ["D.J. M .W. Paridaens"]
    assert 418 not in db.judges
    assert db.judges[440]["aliases"] == ["M. Van der Woude"]

    # Conflated homonym rows removed; 4 disambiguated judges created with exact links
    assert 142 not in db.judges
    assert 116 not in db.judges
    assert 397 not in db.judges
    by_name = {j["full_name"]: j for j in db.judges.values()}
    for expected_name in (
        "Rino Rossi",
        "Lucia Serena Rossi",
        "Thomas Francis O'Higgins",
        "Kevin O'Higgins",
    ):
        assert expected_name in by_name

    kevin_id = by_name["Kevin O'Higgins"]["id"]
    kevin_cases = sorted(cj["case_id"] for cj in db.case_judges if cj["judge_id"] == kevin_id)
    assert kevin_cases == [402, 403]

    # 3. Second --apply pass is a pure no-op (100% idempotent)
    db.execute_calls.clear()
    second = cleanup.run_cleanup(db, apply=True)
    assert second["cjeu_ag_opinion_cleanup"]["planned_updates"] == 0
    assert second["judge_duplicate_merges"]["summary"].get("planned_merges", 0) == 0
    assert second["judge_homonym_splits"]["summary"].get("planned_sources_to_split", 0) == 0
    assert db.execute_calls == []
