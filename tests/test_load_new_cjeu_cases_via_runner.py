"""Tests for the sql-runner loader of new CJEU cases (62_load_new_cjeu_cases_via_runner)."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import sys
import urllib.error
from pathlib import Path

import pandas as pd
import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1]
    / "migration"
    / "sql"
    / "62_load_new_cjeu_cases_via_runner.py"
)
_spec = importlib.util.spec_from_file_location("load_new_cjeu_cases_via_runner", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod  # dataclasses resolve their module at class creation
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _row(**over):
    base = {
        "ecli": "ECLI:EU:C:2024:100",
        "celex": "62022CJ0123;62022CJ0123_SUM",
        "date_publication": "2024-02-08;2024-02-01",
        "work_title": None,
        "language_procedure": "German",
        "judicial_procedure_type": "Reference for a preliminary ruling",
        "delivered_by_court_formation": "Third Chamber",
        "sector": "6",
        "type_procedure": "Reference for a preliminary ruling",
        "date_of_request": "2022-02-21",
        "references_journals": None,
        "case_law_published_in_erecueil": None,
        "local_identifier": None,
        "work_part_of_dossier": None,
        "citations_extra_info": "X v Y; Reference",
        "national_judgement": None,
        "case_law_is_about_case_law_subject_matter": "Taxation",
        "origin_country_or_role_qualifier": "Germany",
        "subject_matter": "Taxation;Value added tax",
        "eurovoc": "VAT",
        "keywords": "VAT",
        "directory_codes": "FISC",
        "judge_rapporteur": "Jarukaitis",
        "case_law_delivered_by_judge": "Jarukaitis;Ziemele",
        "origin_country": "Germany",
        "case_law_defended_by_agent": None,
        "case_law_requested_by_agent": None,
        "commented_by_agent": "European Commission",
        "citing": "62020CJ0001;32006L0112",
        "work_cites_work": "62020CJ0001",
        "cited_by": "62024CC0999",
        "legal_resource": "32006L0112",
        "advocate_general": None,
        "conclusions": None,
        "opinion_advocate_general_joined_to_case_court": None,
        "summary": "Summary of the Judgment\n1. VAT; exemptions ",
        "summary_source": "INFOCURIA_DOCUMENT_CONTENT",
    }
    base.update(over)
    return base


def _write_parquet(tmp_path, rows):
    cols = sorted({k for r in rows for k in r})
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows]).astype("object")
    path = tmp_path / "cases.parquet"
    df.to_parquet(path, index=False)
    return path


class FakeRunner:
    """In-memory stand-in for the sql-runner. Reads are routed by SQL shape;
    writes are recorded (and can be made to fail)."""

    def __init__(self, *, cjeu=None, cases=None, fail=None, stored_edges=(), unresolved=()):
        # cjeu: {ecli: (id, celex)} rows that have a cjeu_document
        # cases: {ecli: (id, celex, sources)} other rows of `cases`
        self.cjeu = cjeu or {}
        self.cases = dict(cases or {})
        for e, (i, c) in self.cjeu.items():
            self.cases.setdefault(e, (i, c, ["CJEU"]))
        self.fail = fail or (lambda sql, params: None)
        self.stored_edges = list(stored_edges)  # (source_ecli, target_celex, rel)
        # (id, target_celex_raw, target_ecli_raw, source_dataset, relation_type)
        self.unresolved = list(unresolved)
        self.queries, self.executes = [], []

    # -- reads --
    def query(self, sql, params=None):
        self.queries.append((sql, params))
        s = " ".join(sql.split())
        if s.startswith("EXPLAIN"):
            return {"ok": True, "rows": [{"QUERY PLAN": "Insert"}]}
        if "JOIN cjeu_document d ON d.case_id = c.id" in s and "c.id > %s" in s:
            rows = [{"id": i, "ecli": e} for e, (i, _) in sorted(self.cjeu.items(), key=lambda x: x[1][0])
                    if i > params[-1]]
            return {"ok": True, "rows": rows[:900]}
        if "FROM language" in s:
            rows = [{"iso_code": x} for x in ("de", "en", "fr") if x > params[-1]]
            return {"ok": True, "rows": rows}
        if "FROM court WHERE" in s:
            return self._lookup(params, [{"id": 1, "code": "CST"}, {"id": 2, "code": "EGC"}, {"id": 3, "code": "CJEU"}])
        if "FROM document_type" in s:
            return self._lookup(params, [{"id": i + 1, "code": c} for i, c in enumerate(
                ["judgment", "decision", "communicated", "opinion", "order", "ruling", "other"])])
        if "FROM court_formation" in s:
            return self._lookup(params, [{"id": i + 1, "code": c} for i, c in enumerate(
                ["GC", "FC", "1C", "2C", "3C", "4C", "5C", "6C", "7C", "8C", "9C", "10C", "PR", "SOLE"])])
        if "FROM procedure_type" in s:
            return self._lookup(params, [{"id": 1, "code": "Reference for a preliminary ruling"}])
        if "FROM judge WHERE" in s:
            return self._lookup(params, [{"id": 1, "full_name": "Jarukaitis"}])
        if "FROM party WHERE" in s:
            return self._lookup(params, [{"id": 1, "canonical_name": "Germany", "role_class": "agent"},
                                         {"id": 2, "canonical_name": "Germany", "role_class": "state"}])
        if "FROM domain WHERE" in s:
            return self._lookup(params, [{"id": 1, "scheme": "eurovoc", "name": "VAT"}])
        if "FROM cases WHERE ecli = ANY" in s:
            return {"ok": True, "rows": [
                {"id": i, "ecli": e, "celex_id": c, "sources": src}
                for e, (i, c, src) in self.cases.items() if e in params[0]]}
        if "FROM cases WHERE celex_id = ANY" in s:
            return {"ok": True, "rows": [
                {"id": i, "ecli": e, "celex_id": c}
                for e, (i, c, _) in self.cases.items() if c and c in params[0]]}
        if "SELECT id, target_celex_raw AS key" in s:
            return {"ok": True, "rows": [
                {"id": i, "key": c, "source_dataset": d, "relation_type": r}
                for i, c, _, d, r in self.unresolved if c in params[0]]}
        if "SELECT id, target_ecli_raw AS key" in s:
            return {"ok": True, "rows": [
                {"id": i, "key": e, "source_dataset": d, "relation_type": r}
                for i, _, e, d, r in self.unresolved if e in params[0]]}
        if "JOIN cases s ON s.id = cc.source_case_id" in s:
            return {"ok": True, "rows": [{"ecli": a, "target": b, "rel": r}
                                         for a, b, r in self.stored_edges if b in params[0]]}
        raise AssertionError(f"unexpected read: {s[:120]}")

    @staticmethod
    def _lookup(params, rows):
        return {"ok": True, "rows": [r for r in rows if r["id"] > params[-1]]}

    # -- writes --
    def execute(self, sql, params=None):
        self.fail(sql, params)
        self.executes.append((sql, params))
        return {"ok": True, "row_count": len(params[0]) if params else 0}


def _prod_state(runner):
    return mod.read_prod_state(runner)


def _df(tmp_path, rows):
    return mod.load_corpus(_write_parquet(tmp_path, rows))


def _label(sql):
    m = re.search(r"(?:INSERT INTO|UPDATE) (\w+)", sql)
    return m.group(1) if m else sql[:30]


# ---------------------------------------------------------------------------
# mapping (must equal 50_load_cjeu.py + 55 as verified live)
# ---------------------------------------------------------------------------

def test_map_row_case_and_document_columns():
    m = mod.map_row(_row())
    assert m.ecli == "ECLI:EU:C:2024:100"
    assert m.celex == "62022CJ0123"          # first ';' token
    assert m.case == {
        "ecli": "ECLI:EU:C:2024:100", "celex": "62022CJ0123",
        "title": "Case C-123/22",            # work_title empty -> synthesized
        "date_decision": "2024-02-01",       # min of date_publication tokens
        "court_code": "CJEU", "lang": "de",  # "German" -> ISO
        "doctype": "judgment", "proc": "Reference for a preliminary ruling",
        "case_number": "C-123/22", "importance": 2,  # Third Chamber (3C) -> 2
    }
    assert m.document["formation_code"] == "3C"
    assert m.document["date_lodged"] == "2022-02-21"
    assert m.document["sector"] == "6"
    assert m.document["citations_extra_info"] == "X v Y; Reference"  # untokenized
    assert m.ag is None and m.national is None


@pytest.mark.parametrize("celex,court,doctype,cn", [
    ("62019TO0100", "EGC", "order", "T-100/19"),
    ("62010FO0042", "CST", "order", "F-42/10"),
    ("62021CO0619", "CJEU", "order", "C-619/21"),
    ("62016CC0418", "CJEU", "opinion", "C-418/16"),
    ("62020CV0001", "CJEU", "ruling", "C-1/20"),
    ("61999CJ0333", "CJEU", "judgment", "C-333/99"),
])
def test_map_row_celex_derived_fields(celex, court, doctype, cn):
    m = mod.map_row(_row(celex=celex))
    assert (m.case["court_code"], m.case["doctype"], m.case["case_number"]) == (court, doctype, cn)
    assert (m.ag is not None) == (doctype == "opinion")


@pytest.mark.parametrize("formation,expected", [
    ("Grand Chamber", 1), ("Full Court", 1), ("First Chamber", 2),
    ("Seventh Chamber", 3), ("President", 4), (None, None),
])
def test_importance_proxy(formation, expected):
    assert mod.map_row(_row(delivered_by_court_formation=formation)).case["importance"] == expected


def test_map_row_fanouts_match_production_representation():
    m = mod.map_row(_row())
    assert m.domains == {
        ("cjeu_is_about_subject", "Taxation"), ("cjeu_subject_matter", "Taxation"),
        ("cjeu_subject_matter", "Value added tax"), ("eurovoc", "VAT"),
        ("cjeu_directory_code", "FISC"),
    }  # `keywords` skipped: production has no cjeu_keyword domains
    assert m.judges == {("Jarukaitis", "rapporteur"), ("Jarukaitis", "judge"), ("Ziemele", "judge")}
    # origin_country -> agent party (50); qualifier -> state party (55)
    assert m.parties == {("Germany", "agent", "referring_state"),
                         ("Germany", "state", "referring_state"),
                         ("European Commission", "agent", "commenting_agent")}
    assert m.citations == {("62020CJ0001", "cites"), ("32006L0112", "cites"),
                           ("62024CC0999", "cited_by")}
    assert m.lawrefs == {("32006L0112", "legal_basis")}


def test_keywords_scheme_opt_in():
    m = mod.map_row(_row(keywords="Customs"), include_keywords=True)
    assert ("cjeu_keyword", "Customs") in m.domains


def test_opinion_maps_ag_row():
    m = mod.map_row(_row(celex="62016CC0418", advocate_general="Sharpston",
                         conclusions="uri;uri2",
                         opinion_advocate_general_joined_to_case_court="http://x/case/C-418%2F16"))
    assert m.ag == {"ecli": m.ecli, "ag": "Sharpston", "opinion_uri": "uri",
                    "parent_raw": "http://x/case/C-418%2F16"}


def test_national_sector8_non_eu_ecli_matches_production_shape():
    # production stores the ~1,620 CJEU-origin sector-8 rows like
    # ECLI:DE:BGH:2018:141118BXII292.16.0 / 82018DE1114(51) with court CJEU,
    # document_type 'other', case_number 'C-1114/18', title = work_title,
    # language NULL (verified live)
    m = mod.map_row(_row(
        ecli=" ecli:de:bgh:2018:141118bxii292.16.0 ", celex="82018DE1114(51)", sector="8",
        work_title="BGH", language_procedure=None, delivered_by_court_formation=None,
        judicial_procedure_type=None,
        case_law_delivered_by_court_national="http://court", case_law_national_parties="A;B"))
    assert m.ecli == "ECLI:DE:BGH:2018:141118BXII292.16.0"
    assert m.case["court_code"] == "CJEU"
    assert m.case["doctype"] == "other"
    assert m.case["case_number"] == "C-1114/18"
    assert m.case["title"] == "BGH"
    assert m.case["lang"] is None
    assert m.national["national_court_uri"] == "http://court"
    assert m.national["national_parties_raw"] == "A"   # first token, as 50
    assert m.document["sector"] == "8"


def test_sector8_without_national_values_has_no_national_row():
    assert mod.map_row(_row(sector="8")).national is None


def test_unparseable_dates_become_null():
    m = mod.map_row(_row(date_publication="not-a-date", date_of_request="??"))
    assert m.case["date_decision"] is None and m.document["date_lodged"] is None


def test_load_corpus_filters_and_dedups(tmp_path):
    df = _df(tmp_path, [
        _row(),
        _row(ecli="ECLI:EU:C:2024:100"),              # duplicate ECLI
        _row(ecli="ecli:eu:c:2024:100 "),             # duplicate after normalization
        _row(ecli=None, celex="62022CJ0200"),         # no ECLI
        _row(ecli="ECLI:EU:C:2024:101", celex=None),  # no CELEX
        _row(ecli="ECLI:EU:C:2024:102", celex="62022CO0124"),
    ])
    assert list(df["x_ecli"]) == ["ECLI:EU:C:2024:100", "ECLI:EU:C:2024:102"]


# ---------------------------------------------------------------------------
# planning
# ---------------------------------------------------------------------------

def test_plan_selects_only_cases_without_cjeu_document(tmp_path):
    df = _df(tmp_path, [
        _row(ecli="ECLI:EU:C:2020:1", celex="62020CJ0001", citing="62022CO0124"),
        _row(ecli="ECLI:EU:C:2024:102", celex="62022CO0124", citing="62020CJ0001"),
    ])
    runner = FakeRunner(cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001"),
                              "ECLI:EU:C:2012:820": (11, "62011CJ0279")})
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert [m.ecli for m in plan.new] == ["ECLI:EU:C:2024:102"]
    assert plan.errors == []
    # existing case cites the new CELEX -> incoming edge
    assert plan.incoming == [("ECLI:EU:C:2020:1", "62022CO0124", "cites")]
    # production rows absent from the corpus are reported, never touched
    assert plan.stats["production_cjeu_missing_from_corpus"] == 1


def test_plan_rejects_celex_owned_by_other_production_case(tmp_path):
    # stale alias: production ECLI:EU:C:2012:820 owns 62011CJ0279
    df = _df(tmp_path, [_row(ecli="ECLI:EU:C:2012:834", celex="62011CJ0279")])
    runner = FakeRunner(cjeu={"ECLI:EU:C:2012:820": (11, "62011CJ0279")})
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert plan.new == []
    assert plan.errors == [("ECLI:EU:C:2012:834",
                            "CELEX 62011CJ0279 already belongs to production case ECLI:EU:C:2012:820")]


def test_plan_rejects_every_member_of_a_shared_celex_group(tmp_path):
    df = _df(tmp_path, [
        _row(ecli="ECLI:EU:C:2004:833", celex="62004CO0244"),
        _row(ecli="ECLI:EU:C:2005:800", celex="62004CO0244"),
        _row(ecli="ECLI:EU:C:2005:801", celex="62004CO0245"),
    ])
    runner = FakeRunner()
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert [m.ecli for m in plan.new] == ["ECLI:EU:C:2005:801"]
    assert sorted(e for e, _ in plan.errors) == ["ECLI:EU:C:2004:833", "ECLI:EU:C:2005:800"]


def test_plan_attaches_to_existing_non_cjeu_case_row(tmp_path):
    # e.g. a Dutch sector-8 decision that already exists as an RS case
    df = _df(tmp_path, [_row(ecli="ECLI:NL:HR:2020:1", celex="82020NL0101(51)", sector="8")])
    runner = FakeRunner(cases={"ECLI:NL:HR:2020:1": (99, None, ["RS"])})
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert [m.ecli for m in plan.new] == ["ECLI:NL:HR:2020:1"]
    assert "ECLI:NL:HR:2020:1" in plan.attach_existing
    counts = mod.planned_counts(plan)
    assert counts["cases (new rows)"] == 0
    assert counts["cases (existing row, CJEU satellite attaches)"] == 1
    assert counts["case_text (summary-only rows; no fulltext)"] == 1


def test_plan_warns_on_non_standard_ecli(tmp_path):
    df = _df(tmp_path, [_row(ecli="CZ:NS:2022:29.NSCR.90.2021.1", celex="82022CZ0101(51)", sector="8")])
    runner = FakeRunner()
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert plan.new[0].ecli == "CZ:NS:2022:29.NSCR.90.2021.1"
    assert any("non-standard ECLI" in w for w in plan.warnings)


def test_plan_reports_new_lookup_values(tmp_path):
    df = _df(tmp_path, [_row(language_procedure="Irish", judge_rapporteur="Pavelin",
                             judicial_procedure_type="Opinion procedure")])
    runner = FakeRunner()
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert plan.lookups["language"] == ["ga"]
    assert plan.lookups["procedure_type"] == ["Opinion procedure"]
    assert "Pavelin" in plan.lookups["judge"]
    assert plan.lookups["court (unmapped)"] == []


def test_citation_resolution_skips_incoming_edges_already_stored(tmp_path):
    df = _df(tmp_path, [
        _row(ecli="ECLI:EU:C:2020:1", celex="62020CJ0001", citing="62022CO0124", cited_by=None),
        _row(ecli="ECLI:EU:C:2020:2", celex="62020CJ0002", citing="62022CO0124", cited_by=None),
        _row(ecli="ECLI:EU:C:2024:102", celex="62022CO0124", citing="62020CJ0001;31999L0001",
             work_cites_work=None, cited_by=None),
    ])
    runner = FakeRunner(
        cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001"), "ECLI:EU:C:2020:2": (12, "62020CJ0002")},
        stored_edges=[("ECLI:EU:C:2020:1", "62022CO0124", "cites")])
    plan = mod.build_plan(df, _prod_state(runner), runner)
    res = mod.citation_resolution(runner, plan)
    assert res["outgoing_edges"] == 2 and res["outgoing_resolved"] == 1
    assert res["incoming_edges_candidate"] == 2
    assert res["incoming_edges_to_insert"] == 1


# ---------------------------------------------------------------------------
# execution (mocked runner)
# ---------------------------------------------------------------------------

def _plan(tmp_path, n=5, runner=None, unresolved=()):
    rows = [_row(ecli="ECLI:EU:C:2020:1", celex="62020CJ0001", citing="62022CO0001")]
    rows += [_row(ecli=f"ECLI:EU:C:2024:{200 + i}", celex=f"62022CO{i + 1:04d}")
             for i in range(n)]
    runner = runner or FakeRunner(cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001")},
                                  unresolved=unresolved)
    plan = mod.build_plan(_df(tmp_path, rows), _prod_state(runner), runner)
    mod.collect_link_candidates(runner, plan)
    return runner, plan


def test_execute_plan_writes_are_conflict_safe_and_only_links_update(tmp_path):
    runner, plan = _plan(tmp_path, unresolved=[
        (500, "62022CO0001", None, "cellar_sparql", "cites"),
        (501, None, "ECLI:EU:C:2024:201", "rs_body_cite", "cites")])
    mod.execute_plan(runner, plan, case_batch=2, row_chunk=3, log=lambda *_: None)
    assert runner.executes
    updates = 0
    for sql, _ in runner.executes:
        s = " ".join(sql.split()).upper()
        assert "DELETE " not in s
        if s.startswith("UPDATE "):
            updates += 1
            assert s.startswith("UPDATE CASE_CITATION CC SET TARGET_CASE_ID = T.ID")
            assert "CC.TARGET_CASE_ID IS NULL" in s and "CC.ID = ANY(" in s
            continue
        assert s.startswith("INSERT INTO ")
        assert "ON CONFLICT" in s or "WHERE NOT EXISTS" in s
        if "CASE_TEXT" in s:  # summary rows only, never fulltext
            assert "FULLTEXT" not in s and "'CELLAR_ITEM'" in s
    assert updates == 2


def test_execute_plan_order_cases_first_and_document_last_per_batch(tmp_path):
    runner, plan = _plan(tmp_path, n=5)
    mod.execute_plan(runner, plan, case_batch=2, row_chunk=1000, log=lambda *_: None)
    labels = [_label(sql) for sql, _ in runner.executes]
    first_sat = min(i for i, l in enumerate(labels)
                    if l not in ("language", "procedure_type", "judge", "party", "domain", "cases"))
    assert max(i for i, l in enumerate(labels) if l == "cases") < first_sat
    assert "case_text" in labels  # summaries go in before the marker
    # 5 cases / batch 2 -> 3 batches, each closed by its cjeu_document insert
    doc_idx = [i for i, l in enumerate(labels) if l == "cjeu_document"]
    assert len(doc_idx) == 3
    assert labels[-1] == "cjeu_document"
    for a, b in zip([first_sat - 1] + doc_idx[:-1], doc_idx):
        assert all(l != "cjeu_document" for l in labels[a + 1:b])
    doc_eclis = [p[0] for sql, p in runner.executes if _label(sql) == "cjeu_document"]
    assert [len(e) for e in doc_eclis] == [2, 2, 1]


def test_execute_plan_chunks_statements_by_row_chunk(tmp_path):
    runner, plan = _plan(tmp_path, n=7)
    mod.execute_plan(runner, plan, case_batch=400, row_chunk=3, log=lambda *_: None)
    case_calls = [p for sql, p in runner.executes if _label(sql) == "cases"]
    assert [len(p[0]) for p in case_calls] == [3, 3, 1]
    assert all(len(p[0]) <= 3 for _, p in runner.executes)


def test_execute_plan_parameters_align_with_placeholders(tmp_path):
    runner, plan = _plan(tmp_path, n=3)
    mod.execute_plan(runner, plan, log=lambda *_: None)
    for sql, params in runner.executes:
        assert sql.count("%s") == len(params), _label(sql)
        if not sql.lstrip().startswith("UPDATE"):
            assert len({len(col) for col in params}) == 1, _label(sql)


def test_incoming_edges_written_with_their_target_batch(tmp_path):
    runner, plan = _plan(tmp_path, n=3)
    mod.execute_plan(runner, plan, case_batch=1, log=lambda *_: None)
    incoming = [p for sql, p in runner.executes if "JOIN cases s ON s.ecli" in sql]
    assert incoming == [[["ECLI:EU:C:2020:1"], ["62022CO0001"], ["cites"]]]


def test_writer_bisects_on_statement_timeout(tmp_path):
    def fail(sql, params):
        if _label(sql) == "cases" and len(params[0]) > 2:
            raise mod.RunnerError(400, '{"detail":{"error":"QueryCanceled"}}')
    runner, plan = _plan(tmp_path, n=5, runner=FakeRunner(
        cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001")}, fail=fail))
    mod.execute_plan(runner, plan, row_chunk=1000, log=lambda *_: None)
    sizes = [len(p[0]) for sql, p in runner.executes if _label(sql) == "cases"]
    assert sum(sizes) == 5 and max(sizes) <= 2


def test_writer_raises_non_splittable_errors(tmp_path):
    def fail(sql, params):
        raise mod.RunnerError(400, '{"detail":{"error":"UndefinedColumn"}}')
    runner, plan = _plan(tmp_path, n=2, runner=FakeRunner(
        cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001")}, fail=fail))
    with pytest.raises(mod.RunnerError, match="UndefinedColumn"):
        mod.execute_plan(runner, plan, log=lambda *_: None)


def test_rerun_after_completion_plans_nothing(tmp_path):
    runner, plan = _plan(tmp_path, n=3)
    mod.execute_plan(runner, plan, log=lambda *_: None)
    # simulate the committed state: every new case now has a cjeu_document
    done = FakeRunner(cjeu={**runner.cjeu, **{m.ecli: (100 + i, m.celex)
                                              for i, m in enumerate(plan.new)}})
    again = mod.build_plan(_df(tmp_path, [_row(ecli=m.ecli, celex=m.celex) for m in plan.new]),
                           _prod_state(done), done)
    assert again.new == [] and again.errors == []


def test_resume_after_crash_before_cjeu_document(tmp_path):
    # crash after `cases` rows were inserted but before cjeu_document:
    # the ECLIs are still "new" and the rerun re-plans them without errors
    runner, plan = _plan(tmp_path, n=2)
    partial = FakeRunner(cjeu=runner.cjeu, cases={
        m.ecli: (200 + i, m.celex, ["CJEU"]) for i, m in enumerate(plan.new)})
    again = mod.build_plan(_df(tmp_path, [_row(ecli=m.ecli, celex=m.celex) for m in plan.new]),
                           _prod_state(partial), partial)
    assert sorted(m.ecli for m in again.new) == sorted(m.ecli for m in plan.new)
    assert again.errors == []
    mod.execute_plan(partial, again, log=lambda *_: None)
    assert sum(1 for sql, _ in partial.executes if _label(sql) == "cjeu_document") == 1


def test_explain_plan_only_reads(tmp_path):
    runner, plan = _plan(tmp_path, n=3)
    n = mod.explain_plan(runner, plan, log=lambda *_: None)
    assert n > 0 and runner.executes == []
    explains = [s for s, _ in runner.queries if s.startswith("EXPLAIN")]
    assert len(explains) == n
    assert all("ANALYZE" not in s for s in explains)


# ---------------------------------------------------------------------------
# transport + dry run
# ---------------------------------------------------------------------------

def test_dry_run_runner_refuses_execute():
    r = mod.Runner("https://runner.invalid", "tok", dry_run=True)
    with pytest.raises(mod.DryRunViolation):
        r.execute("INSERT INTO cases DEFAULT VALUES")
    assert r.calls["execute"] == 0


def test_runner_signs_requests(monkeypatch):
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout):
        seen["req"] = req
        return Resp(json.dumps({"ok": True, "rows": []}).encode())

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    r = mod.Runner("https://runner.invalid/", "secret", dry_run=False)
    r.execute("SELECT 1", [1])
    req = seen["req"]
    body = req.data
    assert json.loads(body) == {"sql": "SELECT 1", "params": [1], "confirm": "execute-cle-v2"}
    h = {k.lower(): v for k, v in req.header_items()}
    msg = "\n".join(["POST", "/execute", h["x-sql-runner-timestamp"], h["x-sql-runner-nonce"],
                     hashlib.sha256(body).hexdigest()]).encode()
    assert h["x-sql-runner-signature"] == "v1=" + hmac.new(b"secret", msg, hashlib.sha256).hexdigest()
    assert "secret" not in json.dumps(dict(req.header_items())) and b"secret" not in body


def test_runner_does_not_retry_deterministic_4xx(monkeypatch):
    calls = []

    def fake_send(self, endpoint, body):
        calls.append(endpoint)
        raise urllib.error.HTTPError("u", 400, "bad", {}, io.BytesIO(b'{"error":"QueryCanceled"}'))

    monkeypatch.setattr(mod.Runner, "_send", fake_send)
    r = mod.Runner("https://runner.invalid", "tok", dry_run=False, sleep=lambda s: None)
    with pytest.raises(mod.RunnerError) as exc:
        r.execute("INSERT ...", [])
    assert exc.value.splittable and len(calls) == 1


def test_runner_retries_transient_errors(monkeypatch):
    calls = []

    def fake_send(self, endpoint, body):
        calls.append(endpoint)
        if len(calls) < 3:
            raise urllib.error.HTTPError("u", 502, "bad gateway", {}, io.BytesIO(b"x"))
        return {"ok": True, "rows": []}

    monkeypatch.setattr(mod.Runner, "_send", fake_send)
    r = mod.Runner("https://runner.invalid", "tok", sleep=lambda s: None)
    assert r.query("SELECT 1")["ok"] and len(calls) == 3


def test_main_dry_run_makes_zero_execute_calls(tmp_path, monkeypatch, capsys):
    path = _write_parquet(tmp_path, [
        _row(ecli="ECLI:EU:C:2020:1", celex="62020CJ0001", citing="62022CO0124"),
        _row(ecli="ECLI:EU:C:2024:102", celex="62022CO0124"),
    ])
    fake = FakeRunner(cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001")})
    endpoints = []

    def fake_send(self, endpoint, body):
        endpoints.append(endpoint)
        payload = json.loads(body)
        assert endpoint == "query" and "confirm" not in payload
        return fake.query(payload["sql"], payload.get("params"))

    monkeypatch.setattr(mod.Runner, "_send", fake_send)
    for k, v in {"SQL_RUNNER_URL": "https://runner.invalid", "SQL_RUNNER_TOKEN": "tok",
                 "CASES_PARQUET": str(path), "EXPLAIN_SQL": "1"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("DRY_RUN", raising=False)   # default must be a dry run
    assert mod.main() == 0
    out = capsys.readouterr().out
    assert "DRY RUN (read-only)" in out and "0 /execute calls" in out
    assert endpoints and set(endpoints) == {"query"}
    assert fake.executes == []


def test_main_write_mode_refuses_on_validation_errors(tmp_path, monkeypatch, capsys):
    path = _write_parquet(tmp_path, [_row(ecli="ECLI:EU:C:2012:834", celex="62011CJ0279")])
    fake = FakeRunner(cjeu={"ECLI:EU:C:2012:820": (11, "62011CJ0279")})
    sent = []

    def fake_send(self, endpoint, body):
        sent.append(endpoint)
        payload = json.loads(body)
        return fake.query(payload["sql"], payload.get("params"))

    monkeypatch.setattr(mod.Runner, "_send", fake_send)
    for k, v in {"SQL_RUNNER_URL": "https://runner.invalid", "SQL_RUNNER_TOKEN": "tok",
                 "CASES_PARQUET": str(path), "DRY_RUN": "0"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("SKIP_INVALID", raising=False)
    assert mod.main() == 2
    assert "refusing to write" in capsys.readouterr().out
    assert "execute" not in sent


# ---------------------------------------------------------------------------
# CELEX shape validation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("celex,ok", [
    ("62022CJ0123", True), ("62004CO0318(01)", True), ("61954CC0001", True),
    ("82025SI1007(51)", True),        # sector-8 national shape, as stored live
    ("82018DE1114(51)", True),
    ("62022CJ0123_SUM", True), ("62022CJ0123_RES", True), ("62022CJ0123_INF", True),
    ("62023TJ0399_EXT", False), ("C-123/22", False), ("6202CJ0123", False),
    ("62022CJ123", False), ("62022CJ0123(1)", False), ("62022cj0123", False),
    ("", False), (None, False),
])
def test_celex_well_formed(celex, ok):
    assert mod.celex_well_formed(celex) is ok


def test_plan_rejects_malformed_celex(tmp_path):
    df = _df(tmp_path, [_row(ecli="ECLI:EU:T:2024:1", celex="62023TJ0399_EXT"),
                        _row(ecli="ECLI:EU:T:2024:2", celex="62023TJ0400")])
    runner = FakeRunner()
    plan = mod.build_plan(df, _prod_state(runner), runner)
    assert [m.ecli for m in plan.new] == ["ECLI:EU:T:2024:2"]
    assert plan.errors == [("ECLI:EU:T:2024:1", "malformed CELEX '62023TJ0399_EXT'")]


# ---------------------------------------------------------------------------
# summaries
# ---------------------------------------------------------------------------

def test_map_row_summary_on_procedure_language():
    m = mod.map_row(_row(summary_source="INFOCURIA_DOCUMENT_CONTENT;CELLAR_SUMMARY_ITEM"))
    assert m.summary == {"ecli": m.ecli, "lang": "de",
                         "summary": "Summary of the Judgment\n1. VAT; exemptions",  # untokenized
                         "summary_source": "INFOCURIA_DOCUMENT_CONTENT"}


def test_map_row_summary_defaults_to_english_and_skips_empty():
    assert mod.map_row(_row(language_procedure=None)).summary["lang"] == "en"
    assert mod.map_row(_row(summary="   ")).summary is None
    assert mod.map_row(_row(summary=None)).summary is None


def test_summary_sql_is_50s_summary_only_row():
    s = " ".join(mod.SQL_SUMMARY.split())
    assert "INSERT INTO case_text (case_id, language, summary, summary_source, source)" in s
    assert "'CELLAR_ITEM'" in s and "fulltext" not in s
    assert "ON CONFLICT (case_id, language, source) DO NOTHING" in s
    assert "ct.source <> 'RECHTSPRAAK'" in s


def test_summaries_chunked_by_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "SUMMARY_BYTES", 250)
    runner, plan = _plan(tmp_path, n=4)
    for m in plan.new:
        m.summary["summary"] = "x" * 100
    mod.execute_plan(runner, plan, log=lambda *_: None)
    sizes = [len(p[0]) for sql, p in runner.executes if _label(sql) == "case_text"]
    assert sizes == [2, 2]


def test_skip_summaries_writes_no_case_text(tmp_path):
    runner, plan = _plan(tmp_path, n=2)
    mod.execute_plan(runner, plan, summaries=False, log=lambda *_: None)
    assert all(_label(sql) != "case_text" for sql, _ in runner.executes)


def _load_sync60():
    os.environ.setdefault("SQL_RUNNER_URL", "https://runner.invalid")
    os.environ.setdefault("SQL_RUNNER_TOKEN", "test-token")
    path = _SCRIPT.parent / "60_sync_cjeu_texts_via_runner.py"
    spec = importlib.util.spec_from_file_location("sync60_for_62_tests", path)
    m60 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m60)  # type: ignore[union-attr]
    return m60


class TextTable:
    """Minimal case_text emulation for the statements 60 issues."""

    def __init__(self, cases):
        self.cases = cases            # ecli -> case_id (cases JOIN cjeu_document)
        self.rows = {}
        self.next_id = 1

    def add(self, **row):
        key = (row["case_id"], row["language"], row["source"])
        if any((r["case_id"], r["language"], r["source"]) == key for r in self.rows.values()):
            return 0                  # UNIQUE (case_id, language, source)
        base = {"fulltext": None, "summary": None, "summary_source": None,
                "text_format": None, "missing_reasons": None}
        self.rows[self.next_id] = {**base, **row, "id": self.next_id}
        self.next_id += 1
        return 1

    def insert_summary(self, m, case_id):
        """What SQL_SUMMARY writes for a new case (no text rows exist yet)."""
        s = m.summary
        return self.add(case_id=case_id, language=s["lang"], summary=s["summary"],
                        summary_source=s["summary_source"], source="CELLAR_ITEM")

    def runner(self, sql, params=None, execute=False):
        s = " ".join(sql.split())
        if s.startswith("SELECT c.id, c.ecli"):
            return {"ok": True, "rows": [{"id": i, "ecli": e, "celex_id": None, "doctype": "judgment"}
                                         for e, i in self.cases.items() if i > params[0]]}
        if "FROM case_text ct JOIN cjeu_document" in s:
            return {"ok": True, "rows": [
                {"id": r["id"], "case_id": r["case_id"], "language": r["language"],
                 "source": r["source"],
                 "fulltext_md5": hashlib.md5((r["fulltext"] or "").encode()).hexdigest()}
                for r in sorted(self.rows.values(), key=lambda r: r["id"]) if r["id"] > params[0]]}
        if s.startswith("INSERT INTO case_text"):
            n = sum(self.add(case_id=c, language=l, fulltext=f or None, source=src,
                             text_format=tf or None, missing_reasons=mr or None,
                             document_kind=kind, document_celex=celex)
                    for c, l, f, src, tf, mr, kind, celex in zip(*params))
            return {"ok": True, "row_count": n}
        if s.startswith("UPDATE case_text ct SET source = v.source"):
            for rid, f, src, tf, mr, kind, celex in zip(*params):
                self.rows[rid].update(source=src, fulltext=f or None,
                                      text_format=tf or None, missing_reasons=mr or None,
                                      document_kind=kind, document_celex=celex)
            return {"ok": True, "row_count": len(params[0])}
        if "SET is_stub" in s:
            return {"ok": True, "row_count": 0}
        raise AssertionError(f"unexpected 60 statement: {s[:100]}")


def _run_sync60(tmp_path, monkeypatch, table, texts):
    m60 = _load_sync60()
    path = tmp_path / "fulltexts.parquet"
    pd.DataFrame(texts, columns=list(m60.COLS)).to_parquet(path, index=False)
    monkeypatch.setenv("FULLTEXTS_PARQUET", str(path))
    for k in ("RECOMPUTE_ONLY", "TARGET_ECLIS_TSV", "TARGET_LANGUAGE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(m60, "runner", table.runner)
    assert m60.main() == 0


def _text(lang, text, source="CELLAR_ITEM", ecli="ECLI:EU:C:2024:100"):
    return {"ecli": ecli, "celex": "62022CJ0123", "text": text, "text_source": source,
            "text_language": lang, "text_format": "xhtml", "missing_reasons": ""}


def test_sync60_fills_fulltext_into_the_summary_row(tmp_path, monkeypatch):
    m = mod.map_row(_row())                      # summary in 'de'
    table = TextTable({m.ecli: 7})
    table.insert_summary(m, 7)
    _run_sync60(tmp_path, monkeypatch, table, [_text("de", "Urteil ..."), _text("en", "Judgment ...")])
    rows = sorted(table.rows.values(), key=lambda r: r["language"])
    assert [(r["language"], r["source"]) for r in rows] == [("de", "CELLAR_ITEM"), ("en", "CELLAR_ITEM")]
    de, en = rows
    assert de["fulltext"] == "Urteil ..." and de["summary"] == m.summary["summary"]
    assert de["summary_source"] == "INFOCURIA_DOCUMENT_CONTENT" and de["text_format"] == "xhtml"
    assert en["summary"] is None
    # synced texts record what they are (migration 0008)
    assert en["document_kind"] == "judgment" and de["document_kind"] == "judgment"
    # idempotent: a second 60 pass changes nothing and adds no rows
    before = {k: dict(v) for k, v in table.rows.items()}
    _run_sync60(tmp_path, monkeypatch, table, [_text("de", "Urteil ..."), _text("en", "Judgment ...")])
    assert table.rows == before


def test_sync60_upgrades_summary_row_for_other_source(tmp_path, monkeypatch):
    m = mod.map_row(_row())
    table = TextTable({m.ecli: 7})
    table.insert_summary(m, 7)
    _run_sync60(tmp_path, monkeypatch, table, [_text("de", "Urteil", source="INFOCURIA_BLOB_HTML")])
    (row,) = table.rows.values()                 # no sibling row created
    assert row["source"] == "INFOCURIA_BLOB_HTML" and row["fulltext"] == "Urteil"
    assert row["summary"] == m.summary["summary"]


def test_loader_summary_insert_is_idempotent_after_sync60(tmp_path, monkeypatch):
    m = mod.map_row(_row())
    table = TextTable({m.ecli: 7})
    assert table.insert_summary(m, 7) == 1
    _run_sync60(tmp_path, monkeypatch, table, [_text("de", "Urteil")])
    assert table.insert_summary(m, 7) == 0       # ON CONFLICT: no duplicate
    assert len(table.rows) == 1


# ---------------------------------------------------------------------------
# citation linking
# ---------------------------------------------------------------------------

def test_collect_link_candidates_classifies_rows(tmp_path):
    runner, plan = _plan(tmp_path, n=2, unresolved=[
        (900, "62022CO0001", None, "cellar_sparql", "cites"),
        (901, "62022CO0002", None, "cellar_sparql", "cited_by"),
        (902, None, "ECLI:EU:C:2024:200", "rs_body_cite", "cites"),
        (903, None, "ECLI:EU:C:2024:201", "rs_formal_relation", "replaced_by"),
        (904, None, "ECLI:EU:C:2024:201", "echr_edge", "cites"),
        (905, None, "ECLI:EU:C:2024:201", "lido", "cites"),       # no rule -> report only
        (906, "62099CO9999", None, "cellar_sparql", "cites"),     # not a new case
    ])
    assert [(c.id, c.kind) for c in plan.links] == [
        (900, "celex"), (901, "celex"), (902, "ecli"), (903, "ecli"), (904, "ecli")]
    assert plan.unlinkable == {"lido:cites:ecli": 1}
    assert mod.link_counts(plan)["cellar_sparql:cites"] == 1


def test_link_updates_follow_batches_and_chunks(tmp_path):
    unresolved = [(1000 + i, "62022CO0001", None, "cellar_sparql", "cites") for i in range(5)]
    unresolved += [(2000, None, "ECLI:EU:C:2024:202", "rs_legacy_ddb", "cites")]
    runner, plan = _plan(tmp_path, n=3, unresolved=unresolved)
    mod.execute_plan(runner, plan, case_batch=1, link_chunk=2, log=lambda *_: None)
    ups = [(sql, p) for sql, p in runner.executes if sql.lstrip().startswith("UPDATE")]
    celex = [p for sql, p in ups if "t.celex_id = cc.target_celex_raw" in sql]
    ecli = [p for sql, p in ups if "t.ecli = cc.target_ecli_raw" in sql]
    assert [p[0] for p in celex] == [[1000, 1001], [1002, 1003], [1004]]
    assert all(p[1] == ["ECLI:EU:C:2024:200"] for p in celex)   # only this batch's targets
    assert ecli == [[[2000], ["ECLI:EU:C:2024:202"]]]
    # the batch's links precede its cjeu_document marker
    labels = [_label(sql) for sql, _ in runner.executes]
    first_doc = labels.index("cjeu_document")
    assert labels.index("case_citation", labels.index("case_text")) < first_doc


def test_link_sql_follows_production_conventions():
    celex = " ".join(mod.SQL_LINK_CELEX.split())
    ecli = " ".join(mod.SQL_LINK_ECLI.split())
    for s in (celex, ecli):
        assert "cc.target_case_id IS NULL" in s          # never overwrite a resolved target
        assert "cc.id = ANY(%s::bigint[])" in s and "t.ecli = ANY(%s::text[])" in s
        assert "NOT EXISTS" in s                          # resolved twin guard
    # cellar_sparql resolved rows keep the raw CELEX and stay non-cross
    assert "SET target_case_id = t.id FROM" in celex and "target_celex_raw =" not in celex.split("WHERE")[0]
    # RS rows: raw ECLI cleared, cross flag as in 40_citations
    assert "target_ecli_raw = NULL" in ecli
    assert "NOT (t.sources @> '{RS}')" in ecli


def test_link_update_bisects_on_row_cap(tmp_path):
    def fail(sql, params):
        if sql.lstrip().startswith("UPDATE") and len(params[0]) > 2:
            raise mod.RunnerError(403, '{"error":"write_row_limit_exceeded"}')
    unresolved = [(1000 + i, "62022CO0001", None, "cellar_sparql", "cites") for i in range(5)]
    runner = FakeRunner(cjeu={"ECLI:EU:C:2020:1": (10, "62020CJ0001")}, fail=fail,
                        unresolved=unresolved)
    runner, plan = _plan(tmp_path, n=1, runner=runner)
    mod.execute_plan(runner, plan, log=lambda *_: None)
    ids = [i for sql, p in runner.executes if sql.lstrip().startswith("UPDATE") for i in p[0]]
    assert sorted(ids) == [1000, 1001, 1002, 1003, 1004]


def test_skip_citation_linking(tmp_path):
    runner, plan = _plan(tmp_path, n=1, unresolved=[(1, "62022CO0001", None, "cellar_sparql", "cites")])
    mod.execute_plan(runner, plan, link=False, log=lambda *_: None)
    assert not any(sql.lstrip().startswith("UPDATE") for sql, _ in runner.executes)


def test_counts_trigger_handles_link_updates():
    # the live trigger (verified via pg_get_functiondef) matches the migration DDL:
    # AFTER INSERT OR UPDATE OR DELETE, and on UPDATE a NULL -> id target change
    # increments the new target's cited_by_count
    ddl = Path("/Users/davidwickerhf/Projects/work/maastricht/citations/caselaw-coolify/"
               "db/migrations/0001_schema_full.sql")
    if not ddl.exists():
        pytest.skip("caselaw-coolify checkout not available")
    s = ddl.read_text()
    assert "AFTER INSERT OR UPDATE OR DELETE ON \"public\".\"case_citation\"" in s
    upd = s[s.index("IF TG_OP = 'UPDATE' THEN"):]
    assert "IF OLD.target_case_id IS DISTINCT FROM NEW.target_case_id THEN" in upd
    assert "cited_by_count = case_citation_counts.cited_by_count + 1" in upd


def test_infocuria_only_case_without_celex_loads_with_null_celex(tmp_path):
    # Consolidation blanks an InfoCuria procedure CELEX shared by several
    # orders; such documents are loaded with a NULL celex_id and described
    # from InfoCuria's procedure CELEX.
    df = _df(tmp_path, [
        _row(ecli="ECLI:EU:C:2007:218", celex=None, infocuria_celex="62007CO0193"),
        _row(ecli="ECLI:EU:C:2007:465", celex=None, infocuria_celex="62007CO0193"),
        _row(ecli="ECLI:EU:C:2007:999", celex=None, infocuria_celex=None),
    ])
    runner = FakeRunner()
    plan = mod.build_plan(df, _prod_state(runner), runner)

    assert sorted(m.ecli for m in plan.new) == ["ECLI:EU:C:2007:218", "ECLI:EU:C:2007:465"]
    assert plan.errors == []
    m = next(m for m in plan.new if m.ecli == "ECLI:EU:C:2007:218")
    assert m.celex is None and m.case["celex"] is None and m.document["celex"] is None
    assert (m.case["court_code"], m.case["doctype"], m.case["case_number"]) == (
        "CJEU", "order", "C-193/07")
