"""Unit tests for R6.1 ECtHR edge v2 staging builder (69_build_echr_edge_v2_staging.py)."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "migration" / "sql" / "69_build_echr_edge_v2_staging.py"
)
SPEC = importlib.util.spec_from_file_location("echr_edge_v2", MODULE_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)


def _cand(
    case_id: int,
    ecli: str,
    date_str: str | None,
    kind: str = "judgment",
    branch: str | None = None,
    secondary_kind: str | None = None,
):
    return mod.TargetCandidate(
        case_id=case_id,
        ecli=ecli,
        item_id=f"001-{case_id}",
        date_decision=dt.date.fromisoformat(date_str) if date_str else None,
        kind=kind,
        branch=branch,
        secondary_kind=secondary_kind,
    )


def test_parse_scl_entry_en_and_fr_dates_and_cues():
    en = mod.parse_scl_entry("Öcalan v. Turkey [GC], no. 46221/99, § 70, 12 May 2005")
    assert en.appnos == ("46221/99",)
    assert en.grand_chamber is True
    assert en.decision_cue is False
    assert en.cited_date == dt.date(2005, 5, 12)
    assert en.cited_year == 2005

    fr = mod.parse_scl_entry("Weber c. Allemagne (déc.), n° 70287/11, §§ 25-28, 1er février 2015")
    assert fr.appnos == ("70287/11",)
    assert fr.decision_cue is True
    assert fr.grand_chamber is False
    assert fr.cited_date == dt.date(2015, 2, 1)

    rev = mod.parse_scl_entry(
        "McGinley and Egan v. the United Kingdom (revision), nos. 21825/93 and 23414/94, § 90, ECHR 2000-I"
    )
    assert rev.appnos == ("21825/93", "23414/94")
    assert rev.secondary_cue == "revision"
    assert rev.cited_year == 2000


def test_appno_from_ecli_recovers_9digit_tail():
    assert mod.appno_from_ecli("ECLI:CE:ECHR:2026:1119JUD003766823") == "37668/23"
    assert mod.appno_from_ecli("ECLI:CE:ECHR:2017:0627JUD000093113") == "931/13"
    assert mod.appno_from_ecli("ECLI:NL:HR:2020:593") is None


def test_choose_target_prefers_judgment_unless_dec_cue():
    dec = _cand(1, "ECLI:CE:ECHR:2000:1214DEC004622199", "2000-12-14", kind="decision")
    jud_ch = _cand(2, "ECLI:CE:ECHR:2003:0312JUD004622199", "2003-03-12", kind="judgment", branch="CHAMBER")
    jud_gc = _cand(3, "ECLI:CE:ECHR:2005:0512JUD004622199", "2005-05-12", kind="judgment", branch="GRANDCHAMBER")
    com = _cand(4, "ECLI:CE:ECHR:1999:0601COM004622199", "1999-06-01", kind="communicated")
    idx = {"46221/99": [com, dec, jud_ch, jud_gc]}

    # Exact date + GC picks the 2005 GC judgment (not 2000 DEC or 2003 Chamber)
    p1 = mod.parse_scl_entry("Öcalan v. Turkey [GC], no. 46221/99, § 70, 12 May 2005")
    cand, ap, rule = mod.choose_target_for_entry(p1, 999, dt.date(2005, 10, 11), idx)
    assert cand.case_id == 3
    assert ap == "46221/99"
    assert "exact_date" in rule

    # (dec.) selects the admissibility decision
    p2 = mod.parse_scl_entry("Öcalan v. Turkey (dec.), no. 46221/99, 14 December 2000")
    cand2, _, rule2 = mod.choose_target_for_entry(p2, 999, dt.date(2005, 10, 11), idx)
    assert cand2.case_id == 1
    assert rule2 == "scl_decision_cue"

    # Future-dated GC excluded when source is from 2004
    p3 = mod.parse_scl_entry("Öcalan v. Turkey, no. 46221/99")
    cand3, _, _ = mod.choose_target_for_entry(p3, 999, dt.date(2004, 1, 1), idx)
    assert cand3.case_id == 2

    # Self-loop excluded
    p4 = mod.parse_scl_entry("Öcalan v. Turkey [GC], no. 46221/99, 12 May 2005")
    cand4, _, _ = mod.choose_target_for_entry(p4, 3, dt.date(2005, 5, 12), idx)
    assert cand4.case_id == 2


def test_choose_target_principal_vs_secondary_judgment():
    merits = _cand(10, "ECLI:CE:ECHR:2005:1006JUD001181003", "2005-10-06", kind="judgment", branch="GRANDCHAMBER")
    strike = _cand(
        11,
        "ECLI:CE:ECHR:2006:0621JUD001181003",
        "2006-06-21",
        kind="judgment",
        branch="GRANDCHAMBER",
        secondary_kind="striking_out",
    )
    just_sat = _cand(
        12,
        "ECLI:CE:ECHR:2005:1220JUD001181003",
        "2005-12-20",
        kind="judgment",
        branch="GRANDCHAMBER",
        secondary_kind="just_satisfaction",
    )
    idx = {"11810/03": [strike, just_sat, merits]}

    # Unqualified citation -> principal merits judgment
    p_plain = mod.parse_scl_entry("Maurice v. France [GC], no. 11810/03")
    c_plain, _, rule = mod.choose_target_for_entry(p_plain, 999, dt.date(2010, 1, 1), idx)
    assert c_plain.case_id == 10
    assert "principal" in rule

    # Explicit just satisfaction cue -> secondary just satisfaction judgment
    p_js = mod.parse_scl_entry("Maurice v. France (just satisfaction) [GC], no. 11810/03")
    c_js, _, rule_js = mod.choose_target_for_entry(p_js, 999, dt.date(2010, 1, 1), idx)
    assert c_js.case_id == 12
    assert "just_satisfaction" in rule_js


def test_classify_secondary_judgment_conclusion_fallback():
    assert (
        mod.classify_secondary_judgment(
            "CASE OF MAURICE v. FRANCE", "Struck out of the list (solution of the matter)"
        )
        == "striking_out"
    )
    assert (
        mod.classify_secondary_judgment(
            "CASE OF MAURICE v. FRANCE",
            "Violation of P1-1;Just satisfaction reserved",
        )
        is None
    )
    assert (
        mod.classify_secondary_judgment(
            "CASE OF HENTRICH v. FRANCE (ARTICLE 50)",
            "Pecuniary damage - financial award",
        )
        == "just_satisfaction"
    )


def test_resolve_case_scl_deduplicates_per_target():
    target = _cand(50, "ECLI:CE:ECHR:2010:0729JUD000329506", "2010-07-29", kind="judgment")
    idx = {"3295/06": [target]}
    scl = "Agraw v. Switzerland, no. 3295/06, § 51, 29 July 2010;Agraw v. Switzerland, no. 3295/06, § 55, 29 July 2010"
    edges, stats = mod.resolve_case_scl(
        source_case_id=100,
        source_ecli="ECLI:CE:ECHR:2012:0912JUD001059308",
        source_item_id="001-113000",
        source_date=dt.date(2012, 9, 12),
        scl_texts=[scl],
        appno_index=idx,
    )
    assert len(edges) == 1
    assert edges[0].target_case_id == 50
    assert stats["scl_entries_dedup_same_target"] == 1


def test_split_scl_handles_missing_semicolons():
    s1 = (
        "Berdzenishvili c. Russie (déc.), n° 31697/03, 29 janvier 2004  "
        "Comm. eur. D.H., n° 23949/94, déc. 18.5.94, DR 77-A, pp. 140, 142"
    )
    parts1 = mod.split_scl(s1)
    assert len(parts1) == 2
    p_comm = mod.parse_scl_entry(parts1[1])
    assert p_comm.appnos == ("23949/94",)
    assert p_comm.decision_cue is True
    assert p_comm.cited_date == dt.date(1994, 5, 18)

    s2 = (
        "Chauvy and Others v. France, no. 64915/01, § 37, ECHR 2004-VI : "
        "Colombani and Others v. France, no. 51279/99, § 55, ECHR 2002-V"
    )
    assert len(mod.split_scl(s2)) == 2

    s3 = (
        "Frydlender v. France [GC], no. 30979/96, § 43, ECHR 2000-VII . "
        "Kudla v. Poland [GC], no. 30210/96, § 156, ECHR 2000-XI"
    )
    assert len(mod.split_scl(s3)) == 2

    s4 = "N° 5577-5583/72, déc. 15.12.75, D.R. 4 p. 4 N° 9564/81 déc. 9.12.83, D.R. 35 p. 87"
    assert len(mod.split_scl(s4)) == 2

    s5 = (
        "Castell c. France (déc.), n° 38783/97, 27.4.1999, "
        "Delgardo c. France (déc.), n° 38437/97, 29.6.1999, "
        "Périé c. France (déc.), n° 38701/97, 11.1.2000"
    )
    assert len(mod.split_scl(s5)) == 3

    s6 = (
        "Altay v. Turkey, no. 22279/93, § 50, 22 May 2001"
        "Berktay v. Turkey, no. 22493/93, § 167, 1 March 2001"
    )
    assert len(mod.split_scl(s6)) == 2

    s7 = (
        "Comm. eur. D.H. No 1103/61, déc. 12.3.62, annuaire 5 pp. 169, 187 "
        "No 5574/72, déc. 21.3.75, D.R. 3 pp. 10, 22 "
        "No 10307/83, déc. 6.3.84, D.R. 37 p. 113"
    )
    assert len(mod.split_scl(s7)) == 3


def test_ecli_primary_appno_breaks_companion_case_tie():
    t_uk = _cand(1, "ECLI:CE:ECHR:1999:1216JUD002472494", "1999-12-16", kind="judgment", branch="GRANDCHAMBER")
    v_uk = _cand(2, "ECLI:CE:ECHR:1999:1216JUD002488894", "1999-12-16", kind="judgment", branch="GRANDCHAMBER")
    idx = {"24888/94": [t_uk, v_uk]}
    p = mod.parse_scl_entry("V. v. the United Kingdom [GC], no. 24888/94, § 71, ECHR 1999-IX")
    chosen, _, rule = mod.choose_target_for_entry(p, 999, dt.date(2005, 1, 1), idx)
    assert chosen.case_id == 2
    assert "ecli_appno" in rule
