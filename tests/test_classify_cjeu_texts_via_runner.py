from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "migration" / "sql" / "63_classify_cjeu_texts_via_runner.py"
_spec = importlib.util.spec_from_file_location("classify_cjeu_texts_via_runner", _SCRIPT)
mod = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = mod
_spec.loader.exec_module(mod)  # type: ignore[union-attr]

TEXTS = {
    ("ECLI:EU:C:2012:361", "en"): ("CELLAR_ITEM", "judgment-md5"),
    ("ECLI:EU:T:2013:545", "es"): ("INFOCURIA_OJ_NOTICE", "notice-md5"),
    ("ECLI:EU:C:2011:784", "en"): ("CELLAR_ITEM", "opinion-md5"),
}
OWNERS = {("en", "judgment-md5"): {"ECLI:EU:C:2012:361"}, ("en", "opinion-md5"): {"ECLI:EU:C:2011:784"}}
CELEX = {"ECLI:EU:C:2011:784": "62010CC0307"}


def _row(**over):
    row = {
        "id": 1, "ecli": "ECLI:EU:C:2012:361", "celex_id": "62010CJ0307",
        "doctype": "judgment", "decision_date": "2012-06-19", "language": "en",
        "source": "CELLAR_ITEM", "md5": "x", "head": "",
    }
    row.update(over)
    return row


def classify(**over):
    return mod.classify(_row(**over), TEXTS, OWNERS, CELEX)


def test_own_corpus_text_is_the_decision():
    assert classify(md5="judgment-md5") == ("judgment", "62010CJ0307", "corpus")


def test_corpus_oj_notice_label_is_kept():
    assert classify(ecli="ECLI:EU:T:2013:545", language="es", md5="notice-md5",
                    celex_id="62013TO0545", doctype="order") == ("oj_notice", "62013TO0545", "corpus")


def test_text_of_another_ecli_is_misfiled():
    # C-307/10: AG Bot's opinion stored as the English text of the judgment.
    assert classify(source="INFOCURIA_BLOB_HTML", md5="opinion-md5") == (
        "misfiled", "62010CC0307", "corpus-other-ecli")


@pytest.mark.parametrize("head,expected", [
    ("62020TJ0483_INF Acórdão do Tribunal Geral ...", ("information", "62020TJ0483", "label")),
    ("62011TO0274_SUM ORDER OF THE GENERAL COURT ...", ("summary", "62011TO0274", "label")),
    ("62019CO0791(01)_RES Predmet C-791/19 R ...", ("summary", "62019CO0791(01)", "label")),
    ("62024TJ0414_EXT ΑΠΟΦΑΣΗ ΤΟΥ ΓΕΝΙΚΟΥ ΔΙΚΑΣΤΗΡΙΟΥ ...", ("extract", "62024TJ0414", "label")),
    ("62010CJ0307_EN Parties Grounds Operative part ...", ("judgment", "62010CJ0307", "label")),
    ("62010CC0307_EN OPINION OF ADVOCATE GENERAL ...", ("misfiled", "62010CC0307", "label-other-celex")),
])
def test_cellar_labels(head, expected):
    assert classify(head=head) == expected


def test_cellar_oj_notice_label():
    head = "2020-04-202004T229201804370-00-TRA-DOC-LT-ARRET_DR-T-0437-18-202004456-07_00 2020 m. balandžio 29 d."
    assert classify(ecli="ECLI:EU:T:2020:159", celex_id="62018TJ0437", head=head)[0] == "oj_notice"


def test_infocuria_text_with_another_date_is_misfiled():
    head = "SENTENZA DELLA CORTE (Quarta Sezione) 24 gennaio 2018 (*) «Inadempimento di uno Stato ..."
    assert classify(source="INFOCURIA_BLOB_HTML", ecli="ECLI:EU:C:2017:552",
                    decision_date="2017-07-13", head=head) == ("misfiled", None, "infocuria-other-date")


def test_infocuria_dated_decision_and_notice():
    decision = "ARRÊT DE LA COUR (grande chambre) 19 juin 2012 (*) «Marques ..."
    notice = "Judgment of the Court (Grand Chamber) of 19 June 2012 – Chartered Institute ... (Case C-307/10)"
    assert classify(source="INFOCURIA_BLOB_HTML", language="fr", head=decision)[0] == "judgment"
    assert classify(source="INFOCURIA_BLOB_HTML", head=notice)[0] == "oj_notice"


def test_rechtspraak_and_empty_rows_stay_unclassified():
    assert classify(source="RECHTSPRAAK") is None
    assert classify(md5=None) is None


def test_provisional_and_unlabelled_cellar_texts():
    provisional = "Edizione provvisoria SENTENZA DEL TRIBUNALE (Quarta Sezione) 19 giugno 2012 (*) « Appalti ..."
    sheet = "Judgment of the Court (Sixth Chamber) of 19 June 2012 – UC v Council (Case C-455/24 P) ( Appeal ..."
    assert classify(language="it", head=provisional)[:1] == ("judgment",)
    assert classify(head=sheet) == ("information", "62010CJ0307", "cellar-dated-information")
    assert classify(head="Something else entirely", decision_date="2012-06-19") is None
