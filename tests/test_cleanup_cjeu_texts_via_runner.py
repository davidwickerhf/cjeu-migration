from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "migration" / "sql" / "64_cleanup_cjeu_texts_via_runner.py"
_spec = importlib.util.spec_from_file_location("cleanup_cjeu_texts_via_runner", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]


def _case(case_id, ecli, celex, number="C-1/10", date="2012-06-19", doctype="judgment"):
    return ecli, {"id": case_id, "ecli": ecli, "celex_id": celex, "case_number": number,
                  "decision_date": date, "doctype": doctype}


def _text(text_id, case_id, language="en", source="INFOCURIA_BLOB_HTML", md5="m",
          has_summary=False, kind=None):
    return text_id, {"id": text_id, "case_id": case_id, "language": language, "source": source,
                     "md5": md5, "has_summary": has_summary, "document_kind": kind}


def _entry(text_id, kind, rule="corpus", celex="", head=""):
    return {"id": str(text_id), "kind": kind, "rule": rule, "celex": celex, "head": head}


CASES = dict([
    _case(1, "ECLI:EU:C:2012:361", "62010CJ0307"),
    _case(2, "ECLI:EU:C:2013:10", "62010CO0001_SUM", date="2013-01-10", doctype="order"),
    _case(3, "ECLI:EU:C:2011:784", "62010CC0307", date="2011-11-29", doctype="opinion"),
])


def test_plan_drops_duplicates_moves_identified_texts_and_keeps_the_rest():
    texts = dict([
        # opinion text under the judgment, identical to the opinion case's own text
        _text(10, 1, md5="opinion", has_summary=True),
        _text(11, 1, source="CELLAR_ITEM", md5="judgment"),
        _text(30, 3, source="CELLAR_ITEM", md5="opinion"),
        # a 2013 order text under the judgment; the order case lacks German
        _text(12, 1, language="de", md5="order-de"),
        # unidentifiable other document, kept
        _text(13, 1, language="fr", md5="mystery"),
    ])
    classification = [
        _entry(10, "misfiled", "corpus-other-ecli", "62010CC0307"),
        _entry(11, "judgment", "corpus", "62010CJ0307"),
        _entry(30, "opinion", "corpus", "62010CC0307"),
        _entry(12, "misfiled", "infocuria-other-date",
               head="BESCHLUSS DES GERICHTSHOFS 10. Januar 2013 (*) ..."),
        _entry(13, "misfiled", "infocuria-other-date", head="ARRÊT DE LA COUR 3 mars 1999 ..."),
    ]
    plan = mod.build_plan(classification, CASES, texts)

    assert plan["drops"] == [10]
    assert plan["copy_summary"] == [(11, 10)]          # summary stays with the judgment
    assert plan["moves"] == [(12, 2, "order", "62010CO0001")]
    assert dict(plan["kept"]) == {"owner unknown": 1}
    assert plan["celex_fixes"] == [(2, "62010CO0001_SUM", "62010CO0001")]


def test_plan_keeps_a_summary_only_row_when_nothing_else_remains():
    texts = dict([_text(10, 1, md5="opinion", has_summary=True), _text(30, 3, source="CELLAR_ITEM", md5="opinion")])
    classification = [_entry(10, "misfiled", "corpus-other-ecli", "62010CC0307")]
    plan = mod.build_plan(classification, CASES, texts)

    assert plan["convert"] == [10] and plan["drops"] == []


def test_document_celex_keeps_derived_suffix_from_label():
    assert mod.document_celex(_entry(1, "summary", "label", "62019CO0791(01)",
                                     "62019CO0791(01)_RES Predmet ...")) == "62019CO0791(01)_RES"
    assert mod.document_celex(_entry(1, "judgment", "corpus", "62015CJ0005_SUM")) == "62015CJ0005"
