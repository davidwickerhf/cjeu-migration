# R6.1 ECtHR Edge v2 Staging Gate Report (`r6.1-v3`)

## 1. Global Parity & Network Metrics (Before vs After)

| Metric | Legacy `echr_edge` | Staged `echr_edge_v2` | Delta |
|---|---:|---:|---:|
| Resolved edges | 428,440 | 157,972 | -270,468 |
| Distinct source cases | 19,129 | 19,304 | +175 |
| Distinct target cases | 44,003 | 18,832 | -25,171 |
| Mean out-degree | 22.40 | 8.18 | -14.22 |
| p50 / p90 / p99 out-degree | 7 / 36 / 91 | 4 / 21 / 51 | — |
| Max out-degree | 15,597 | 205 | -15,392 |
| Cases with >100 outgoing edges | 151 | 14 | -137 |
| Cases with >300 outgoing edges (`case_cited_by` cutoff) | 54 | 0 | -54 |
| Edges trapped in >300 out-degree cases | 164,177 | 0 | -164,177 |

- **Kept:** `155,445`
- **Dropped (total):** `272,995` (`269,068` on sources with `scl`; `3,927` on 148 post-Oct-27 weekly-ETL sources whose `echr_document.scl` is NULL because `PostgresECHRProcessor._detail_row` did not persist `scl`)
- **Newly added (recovered from `scl`):** `2,527`

## 2. Classification of ALL Dropped Legacy Edges

| Drop Reason | Rows | % of Drops | Sampled in Gate |
|---|---:|---:|---:|
| `non_owner_appno_fanout` | 162,214 | 59.42% | 95 |
| `wrong_doctype_decision_without_dec_cue` | 78,718 | 28.83% | 59 |
| `extra_judgment_for_same_appno` | 17,837 | 6.53% | 33 |
| `wrong_doctype_judgment_for_dec_cue` | 4,136 | 1.52% | 27 |
| `source_no_scl` | 3,927 | 1.44% | 0 |
| `future_target_date` | 3,045 | 1.12% | 26 |
| `extra_decision_for_same_appno` | 2,887 | 1.06% | 26 |
| `unmatched_with_owner_appno_in_scl` | 173 | 0.06% | 25 |
| `self_loop` | 54 | 0.02% | 25 |
| `communicated_target` | 4 | 0.00% | 4 |

## 3. Top 15 Legacy Out-Degree Explosions (Before vs After)

| Source ECLI | Date | Legacy Out-Deg | v2 Out-Deg | Dropped |
|---|---|---:|---:|---:|
| `ECLI:CE:ECHR:2025:1211JUD001318620` | 2025-12-11 | 15,597 | 205 | 15,392 |
| `ECLI:CE:ECHR:2025:1113JUD000377217` | 2025-11-13 | 9,091 | 30 | 9,061 |
| `ECLI:CE:ECHR:2025:1216JUD001350520` | 2025-12-16 | 7,330 | 79 | 7,251 |
| `ECLI:CE:ECHR:2026:0108JUD002353814` | 2026-01-08 | 7,313 | 92 | 7,221 |
| `ECLI:CE:ECHR:2025:1211JUD001558710` | 2025-12-11 | 6,908 | 33 | 6,875 |
| `ECLI:CE:ECHR:2025:1118JUD005300221` | 2025-11-18 | 6,690 | 41 | 6,649 |
| `ECLI:CE:ECHR:2025:1215JUD001691521` | 2025-12-15 | 6,653 | 71 | 6,582 |
| `ECLI:CE:ECHR:2025:1127JUD004305022` | 2025-11-27 | 6,243 | 36 | 6,207 |
| `ECLI:CE:ECHR:2025:1104JUD001798221` | 2025-11-04 | 6,186 | 24 | 6,162 |
| `ECLI:CE:ECHR:2025:1106JUD002560820` | 2025-11-06 | 4,777 | 43 | 4,734 |
| `ECLI:CE:ECHR:2025:1113JUD000603021` | 2025-11-13 | 4,207 | 25 | 4,182 |
| `ECLI:CE:ECHR:2026:0115JUD003270719` | 2026-01-15 | 3,916 | 32 | 3,884 |
| `ECLI:CE:ECHR:2025:1216JUD003470121` | 2025-12-16 | 3,732 | 27 | 3,705 |
| `ECLI:CE:ECHR:2025:1211JUD004656919` | 2025-12-11 | 3,300 | 20 | 3,280 |
| `ECLI:CE:ECHR:2025:1113JUD001968921` | 2025-11-13 | 3,031 | 26 | 3,005 |

## 4. Disambiguation Rule Breakdown (`echr_edge_v2_staging`)

| Rule | Rows |
|---|---:|
| `scl_judgment` | 103,914 |
| `scl_decision_cue` | 25,850 |
| `scl_judgment_year` | 13,103 |
| `scl_judgment_exact_date` | 8,806 |
| `scl_decision_cue_exact_date` | 2,660 |
| `scl_decision_no_judgment` | 1,554 |
| `scl_judgment_year_gc_principal_ecli_appno` | 408 |
| `scl_decision_cue_year` | 299 |
| `scl_judgment_year_principal_ecli_appno` | 285 |
| `scl_judgment_exact_date_year_principal_ecli_appno` | 260 |
| `scl_decision_cue_year_principal_earliest` | 126 |
| `scl_judgment_year_principal` | 110 |
| `scl_decision_cue_principal_earliest` | 99 |
| `scl_decision_no_judgment_exact_date` | 72 |
| `scl_judgment_gc_just_satisfaction` | 54 |
| `scl_decision_cue_exact_date_year_principal_earliest` | 51 |
| `scl_judgment_principal` | 48 |
| `scl_judgment_gc` | 45 |
| `scl_decision_no_judgment_principal_earliest` | 39 |
| `scl_judgment_principal_earliest` | 34 |
| `scl_judgment_exact_date_year_gc_principal_ecli_appno` | 34 |
| `scl_judgment_year_gc_principal` | 34 |
| `scl_judgment_principal_ecli_appno` | 15 |
| `scl_decision_cue_exact_date_year_principal_ecli_appno` | 13 |
| `scl_judgment_exact_date_year_principal_earliest` | 13 |
| `scl_decision_no_judgment_year` | 12 |
| `scl_judgment_gc_principal` | 8 |
| `scl_judgment_year_earliest` | 6 |
| `scl_decision_no_judgment_exact_date_year_principal_earliest` | 4 |
| `scl_decision_cue_principal_ecli_appno` | 3 |
| `scl_decision_no_judgment_year_principal_earliest` | 3 |
| `scl_judgment_just_satisfaction` | 2 |
| `scl_judgment_year_gc_principal_earliest` | 2 |
| `scl_judgment_year_principal_earliest` | 2 |
| `scl_decision_no_judgment_principal_ecli_appno_earliest` | 1 |
| `scl_judgment_gc_principal_ecli_appno` | 1 |
| `scl_judgment_earliest` | 1 |
| `scl_decision_no_judgment_exact_date_year_principal_ecli_appno` | 1 |

## 5. Stratified Drop Gate Sample (320 rows, seed=20261126)

Full JSON artifact with all 320 dropped rows, 60 kept rows, and 30 added rows saved to `r6_1_echr_edge_v2_gate.json`.

| # | Reason | Source ECLI | Target ECLI (Date, Kind) | Evidence / `scl` Excerpt |
|---:|---|---|---|---|
| 1 | `communicated_target` | `ECLI:CE:ECHR:2026:1022JUD000868314` | `None` (None, communicated) | target is communicated case (None) |
| 2 | `communicated_target` | `ECLI:CE:ECHR:2026:0723JUD001534520` | `None` (None, communicated) | target is communicated case (None) |
| 3 | `communicated_target` | `ECLI:CE:ECHR:2026:0625JUD000047421` | `None` (None, communicated) | target is communicated case (None) |
| 4 | `communicated_target` | `ECLI:CE:ECHR:2026:0825JUD000217024` | `None` (None, communicated) | target is communicated case (None) |
| 5 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0119DEC001944302` | `ECLI:CE:ECHR:2000:0427DEC004939299` (2000-04-27, decision) | v2 chose ECLI:CE:ECHR:2001:0918DEC004939299 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0427DEC004939299; scl='Debbasch c. France (déc.), no 49392/99,  |
| 6 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0522DEC000803505` | `ECLI:CE:ECHR:1998:1027DEC003952198` (1998-10-27, decision) | v2 chose ECLI:CE:ECHR:1999:1005DEC003952198 (scl_decision_cue_year) over ECLI:CE:ECHR:1998:1027DEC003952198; scl='González Marín c. Espagne (déc.), no 39521/98, |
| 7 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2020:0707JUD000529414` | `ECLI:CE:ECHR:1996:0228DEC002331394` (1996-02-28, decision) | v2 chose ECLI:CE:ECHR:1995:0518DEC002331394 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1996:0228DEC002331394; scl='Erbs v. France, no. 23313/94, Commission |
| 8 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2005:0217JUD005627100` | `ECLI:CE:ECHR:1998:0304DEC003109396` (1998-03-04, decision) | v2 chose ECLI:CE:ECHR:1997:0226DEC003109396 (scl_decision_cue_principal_earliest) over ECLI:CE:ECHR:1998:0304DEC003109396; scl='Schlader v. Austria (Dec.), no.  |
| 9 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2019:0827JUD003263109` | `ECLI:CE:ECHR:2008:0104DEC004552004` (2008-01-04, decision) | v2 chose ECLI:CE:ECHR:2014:1125DEC004552004 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2008:0104DEC004552004; scl='Larionovs and Tess v. Latvia (dec.), nos |
| 10 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2019:0124JUD007657713` | `ECLI:CE:ECHR:2000:0829DEC004423498` (2000-08-29, decision) | v2 chose ECLI:CE:ECHR:2002:0124DEC004423498 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0829DEC004423498; scl='Uçak v. the United Kingdom (dec.), no 44 |
| 11 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0202DEC005146699` | `ECLI:CE:ECHR:2003:1007DEC007175201` (2003-10-07, decision) | v2 chose ECLI:CE:ECHR:2003:0114DEC007175201 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:1007DEC007175201; scl='Quiles Gonzalez c. Espagne (déc.), no 71 |
| 12 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2002:0903DEC006760301` | `ECLI:CE:ECHR:2000:0302DEC004542499` (2000-03-02, decision) | v2 chose ECLI:CE:ECHR:2000:1212DEC004542499 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0302DEC004542499; scl='Truhli c. Croatie (déc.), n° 45424/99, 1 |
| 13 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2007:0412JUD006377800` | `ECLI:CE:ECHR:1996:1021DEC002922195` (1996-10-21, decision) | v2 chose ECLI:CE:ECHR:1998:0629DEC002922195 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1996:1021DEC002922195; scl='Stankov and United Macedonian Organisati |
| 14 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0711DEC001805906` | `ECLI:CE:ECHR:1998:0414DEC003141496` (1998-04-14, decision) | v2 chose ECLI:CE:ECHR:1999:0112DEC003141496 (scl_decision_cue_year) over ECLI:CE:ECHR:1998:0414DEC003141496; scl='Karassev v. Finland (dec.), no. 31414/96, ECHR |
| 15 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2005:0628DEC001382802` | `ECLI:CE:ECHR:2002:0611DEC003866597` (2002-06-11, decision) | v2 chose ECLI:CE:ECHR:1999:0601DEC003866597 (scl_decision_cue_principal_earliest) over ECLI:CE:ECHR:2002:0611DEC003866597; scl='Bukowski v. Poland (dec.), no. 3 |
| 16 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2013:0219JUD003828509` | `ECLI:CE:ECHR:1998:0414DEC002752695` (1998-04-14, decision) | v2 chose ECLI:CE:ECHR:2002:0425DEC002752695 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1998:0414DEC002752695; scl='Günaydin v.  Turkey (dec.), no 27526/95, |
| 17 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2003:0513DEC005186699` | `ECLI:CE:ECHR:2000:1019DEC004371698` (2000-10-19, decision) | v2 chose ECLI:CE:ECHR:2002:1008DEC004371698 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:1019DEC004371698; scl='Susini et autres c. France (déc.), n° 43 |
| 18 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2012:0911DEC002000709` | `ECLI:CE:ECHR:1999:0202DEC003735797` (1999-02-02, decision) | v2 chose ECLI:CE:ECHR:1998:0701DEC003735797 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1999:0202DEC003735797; scl='Pedro Garcia Sanchez c. Espagne, no 3735 |
| 19 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2021:0518JUD004892416` | `ECLI:CE:ECHR:1997:0528DEC002632395` (1997-05-28, decision) | v2 chose ECLI:CE:ECHR:1999:0601DEC002632395 (scl_decision_cue_year) over ECLI:CE:ECHR:1997:0528DEC002632395; scl='Kiiskinen v. Finland (dec.), no 26323/95, ECHR |
| 20 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2014:0904JUD000014010` | `ECLI:CE:ECHR:2000:1214DEC004419098` (2000-12-14, decision) | v2 chose ECLI:CE:ECHR:2001:0703DEC004419098 (scl_decision_cue_year) over ECLI:CE:ECHR:2000:1214DEC004419098; scl='Nivette v. France (dec.), no 44190/98, ECHR 20 |
| 21 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2007:0220JUD006571501` | `ECLI:CE:ECHR:2000:0314DEC003970698` (2000-03-14, decision) | v2 chose ECLI:CE:ECHR:2000:1107DEC003970698 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0314DEC003970698; scl='Tahsin Ipek v. Turkey (dec.), no. 39706/ |
| 22 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:1130JUD003237402` | `ECLI:CE:ECHR:2003:1216DEC002943902` (2003-12-16, decision) | v2 chose ECLI:CE:ECHR:2002:1126DEC002943902 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:1216DEC002943902; scl='Sokur v. Ukraine (dec.), no. 29439/02, 2 |
| 23 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2025:0304JUD007500017` | `ECLI:CE:ECHR:2024:0118DEC000213423` (2024-01-18, decision) | v2 chose ECLI:CE:ECHR:2023:0606DEC000213423 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2024:0118DEC000213423; scl='Pivkina and Others v. Russia (dec.), nos |
| 24 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:1005JUD002951004` | `ECLI:CE:ECHR:2003:0304DEC004005798` (2003-03-04, decision) | v2 chose ECLI:CE:ECHR:2004:0518DEC004005798 (scl_decision_cue_year) over ECLI:CE:ECHR:2003:0304DEC004005798; scl='Des Fours Walderode v. the Czech Republic (dec |
| 25 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:1010JUD006912901` | `ECLI:CE:ECHR:2001:1016DEC004209698` (2001-10-16, decision) | v2 chose ECLI:CE:ECHR:2003:0304DEC004209698 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2001:1016DEC004209698; scl='Skawinska v. Poland (dec.), no. 42096/98 |
| 26 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2004:1118DEC004546699` | `ECLI:CE:ECHR:2000:0302DEC004407098` (2000-03-02, decision) | v2 chose ECLI:CE:ECHR:2001:0705DEC004407098 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0302DEC004407098; scl='Beljanski c. France (déc.), n° 44070/98, |
| 27 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2011:0127JUD004222402` | `ECLI:CE:ECHR:2005:0512DEC001489904` (2005-05-12, decision) | v2 chose ECLI:CE:ECHR:2009:0108DEC001489904 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2005:0512DEC001489904; scl='Babkin v. Russia (dec.), no. 14899/04, 8 |
| 28 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2001:0906DEC002633795` | `ECLI:CE:ECHR:2000:0404DEC002518294` (2000-04-04, decision) | v2 chose ECLI:CE:ECHR:1995:1024DEC002518294 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0404DEC002518294; scl='Comm. Eur. D.H.  Nos 25182/94, 26956/95, |
| 29 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2014:1125DEC004552004` | `ECLI:CE:ECHR:2011:0215DEC003849405` (2011-02-15, decision) | v2 chose ECLI:CE:ECHR:2013:0924DEC003849405 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2011:0215DEC003849405; scl='Ignats v. Latvia (dec.), no. 38494/05, § |
| 30 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2005:0510DEC001935402` | `ECLI:CE:ECHR:2001:0619DEC005783600` (2001-06-19, decision) | v2 chose ECLI:CE:ECHR:2003:0130DEC005783600 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2001:0619DEC005783600; scl='Mellors v. the United Kingdom (dec.), no |
| 31 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2009:0423JUD001437003` | `ECLI:CE:ECHR:2001:1011JUD003094396` (2001-10-11, judgment) | v2 chose ECLI:CE:ECHR:2003:0708JUD003094396 (scl_judgment_year) over ECLI:CE:ECHR:2001:1011JUD003094396 (date=2001-10-11, branch=None, sec=None); scl='Sahin v.  |
| 32 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:1013JUD002814310` | `ECLI:CE:ECHR:2012:0828JUD001715311` (2012-08-28, judgment) | v2 chose ECLI:CE:ECHR:2014:0325JUD001715311 (scl_judgment_exact_date) over ECLI:CE:ECHR:2012:0828JUD001715311 (date=2012-08-28, branch=None, sec=None); scl='Vuč |
| 33 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2021:1123JUD004044417` | `ECLI:CE:ECHR:2017:0214JUD003648007` (2017-02-14, judgment) | v2 chose ECLI:CE:ECHR:2018:1211JUD003648007 (scl_judgment_exact_date) over ECLI:CE:ECHR:2017:0214JUD003648007 (date=2017-02-14, branch=None, sec=None); scl='Lek |
| 34 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2016:0426JUD001992013` | `ECLI:CE:ECHR:2001:0731JUD004134098` (2001-07-31, judgment) | v2 chose ECLI:CE:ECHR:2003:0213JUD004134098 (scl_judgment_year) over ECLI:CE:ECHR:2001:0731JUD004134098 (date=2001-07-31, branch=None, sec=None); scl='Refah Par |
| 35 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2025:1023JUD001680321` | `ECLI:CE:ECHR:2017:0214JUD003648007` (2017-02-14, judgment) | v2 chose ECLI:CE:ECHR:2018:1211JUD003648007 (scl_judgment_exact_date) over ECLI:CE:ECHR:2017:0214JUD003648007 (date=2017-02-14, branch=None, sec=None); scl='Lek |
| 36 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2021:0121JUD001536714` | `ECLI:CE:ECHR:2019:0529JUD001517213` (2019-05-29, judgment) | v2 chose ECLI:CE:ECHR:2014:0522JUD001517213 (scl_judgment_exact_date) over ECLI:CE:ECHR:2019:0529JUD001517213 (date=2019-05-29, branch=GRANDCHAMBER, sec=None);  |
| 37 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2016:0614JUD003521409` | `ECLI:CE:ECHR:2006:1212JUD001337805` (2006-12-12, judgment) | v2 chose ECLI:CE:ECHR:2008:0429JUD001337805 (scl_judgment_year) over ECLI:CE:ECHR:2006:1212JUD001337805 (date=2006-12-12, branch=None, sec=None); scl='Burden v. |
| 38 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2005:1117JUD006363200` | `ECLI:CE:ECHR:2001:0123JUD002834295` (2001-01-23, judgment) | v2 chose ECLI:CE:ECHR:1999:1028JUD002834295 (scl_judgment_year) over ECLI:CE:ECHR:2001:0123JUD002834295 (date=2001-01-23, branch=GRANDCHAMBER, sec=just_satisfac |
| 39 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2022:0621JUD001042519` | `ECLI:CE:ECHR:2017:0202JUD001021112` (2017-02-02, judgment) | v2 chose ECLI:CE:ECHR:2018:1204JUD001021112 (scl_judgment_exact_date) over ECLI:CE:ECHR:2017:0202JUD001021112 (date=2017-02-02, branch=None, sec=None); scl='Iln |
| 40 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2018:0118JUD004815111` | `ECLI:CE:ECHR:2006:1121JUD003450397` (2006-11-21, judgment) | v2 chose ECLI:CE:ECHR:2008:1112JUD003450397 (scl_judgment_year) over ECLI:CE:ECHR:2006:1121JUD003450397 (date=2006-11-21, branch=None, sec=None); scl='Demir and |
| 41 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2005:0715JUD006324200` | `ECLI:CE:ECHR:2003:1211JUD002463894` (2003-12-11, judgment) | v2 chose ECLI:CE:ECHR:2000:0530JUD002463894 (scl_judgment_year) over ECLI:CE:ECHR:2003:1211JUD002463894 (date=2003-12-11, branch=None, sec=just_satisfaction); s |
| 42 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2012:0626JUD002682806` | `ECLI:CE:ECHR:2007:0322JUD000163803` (2007-03-22, judgment) | v2 chose ECLI:CE:ECHR:2008:0623JUD000163803 (scl_judgment_year) over ECLI:CE:ECHR:2007:0322JUD000163803 (date=2007-03-22, branch=None, sec=None); scl='Maslov v. |
| 43 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:1003JUD003927215` | `ECLI:CE:ECHR:2021:0112JUD003634516` (2021-01-12, judgment) | v2 chose ECLI:CE:ECHR:2023:0309JUD003634516 (scl_judgment_exact_date) over ECLI:CE:ECHR:2021:0112JUD003634516 (date=2021-01-12, branch=None, sec=None); scl='L.B |
| 44 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:0213JUD000867515` | `ECLI:CE:ECHR:2017:0404JUD003692507` (2017-04-04, judgment) | v2 chose ECLI:CE:ECHR:2019:0129JUD003692507 (scl_judgment_exact_date) over ECLI:CE:ECHR:2017:0404JUD003692507 (date=2017-04-04, branch=None, sec=None); scl='Güz |
| 45 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2012:1019JUD004337004` | `ECLI:CE:ECHR:2004:0629JUD004477498` (2004-06-29, judgment) | v2 chose ECLI:CE:ECHR:2005:1110JUD004477498 (scl_judgment_year) over ECLI:CE:ECHR:2004:0629JUD004477498 (date=2004-06-29, branch=None, sec=None); scl='Leyla Sah |
| 46 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2010:1007JUD001277303` | `ECLI:CE:ECHR:2002:0618JUD004893999` (2002-06-18, judgment) | v2 chose ECLI:CE:ECHR:2004:1130JUD004893999 (scl_judgment_year) over ECLI:CE:ECHR:2002:0618JUD004893999 (date=2002-06-18, branch=None, sec=None); scl='Öneryildi |
| 47 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2010:1102JUD004172306` | `ECLI:CE:ECHR:2007:0322JUD000163803` (2007-03-22, judgment) | v2 chose ECLI:CE:ECHR:2008:0623JUD000163803 (scl_judgment_exact_date) over ECLI:CE:ECHR:2007:0322JUD000163803 (date=2007-03-22, branch=None, sec=None); scl='Mas |
| 48 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2014:0325JUD001715311` | `ECLI:CE:ECHR:2004:1110JUD005658100` (2004-11-10, judgment) | v2 chose ECLI:CE:ECHR:2006:0301JUD005658100 (scl_judgment_year) over ECLI:CE:ECHR:2004:1110JUD005658100 (date=2004-11-10, branch=None, sec=None); scl='Sejdovic  |
| 49 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:0505DEC000359918` | `ECLI:CE:ECHR:2017:0404JUD003692507` (2017-04-04, judgment) | v2 chose ECLI:CE:ECHR:2019:0129JUD003692507 (scl_judgment_exact_date) over ECLI:CE:ECHR:2017:0404JUD003692507 (date=2017-04-04, branch=None, sec=None); scl='Güz |
| 50 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:1121DEC002524020` | `ECLI:CE:ECHR:2018:1120JUD001430517` (2018-11-20, judgment) | v2 chose ECLI:CE:ECHR:2020:1222JUD001430517 (scl_judgment_exact_date) over ECLI:CE:ECHR:2018:1120JUD001430517 (date=2018-11-20, branch=None, sec=None); scl='Sel |
| 51 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:0204JUD008074917` | `ECLI:CE:ECHR:2016:0628JUD002276812` (2016-06-28, judgment) | v2 chose ECLI:CE:ECHR:2018:0320JUD003768510 (scl_judgment_exact_date) over ECLI:CE:ECHR:2016:0628JUD002276812 (date=2016-06-28, branch=None, sec=None); scl='Rad |
| 52 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2022:0111JUD007887313` | `ECLI:CE:ECHR:2014:0527JUD002026112` (2014-05-27, judgment) | v2 chose ECLI:CE:ECHR:2016:0623JUD002026112 (scl_judgment_exact_date) over ECLI:CE:ECHR:2014:0527JUD002026112 (date=2014-05-27, branch=None, sec=None); scl='Bak |
| 53 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:0706JUD002118119` | `ECLI:CE:ECHR:2018:0206JUD002172211` (2018-02-06, judgment) | v2 chose ECLI:CE:ECHR:2013:0109JUD002172211 (scl_judgment_year) over ECLI:CE:ECHR:2018:0206JUD002172211 (date=2018-02-06, branch=None, sec=striking_out); scl='O |
| 54 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2022:1122JUD004439415` | `ECLI:CE:ECHR:2013:1121JUD002338009` (2013-11-21, judgment) | v2 chose ECLI:CE:ECHR:2015:0928JUD002338009 (scl_judgment_year) over ECLI:CE:ECHR:2013:1121JUD002338009 (date=2013-11-21, branch=None, sec=None); scl='Bouyid v. |
| 55 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2008:0724JUD003545004` | `ECLI:CE:ECHR:2001:0725JUD004889899` (2001-07-25, judgment) | v2 chose ECLI:CE:ECHR:2003:0506JUD004889899 (scl_judgment_year) over ECLI:CE:ECHR:2001:0725JUD004889899 (date=2001-07-25, branch=None, sec=None); scl='Perna v.  |
| 56 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:0119JUD003266719` | `ECLI:CE:ECHR:2005:1011JUD007304901` (2005-10-11, judgment) | v2 chose ECLI:CE:ECHR:2007:0111JUD007304901 (scl_judgment_year) over ECLI:CE:ECHR:2005:1011JUD007304901 (date=2005-10-11, branch=None, sec=None); scl='Anheuser- |
| 57 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2025:1028JUD003406821` | `ECLI:CE:ECHR:2016:0628JUD002276812` (2016-06-28, judgment) | v2 chose ECLI:CE:ECHR:2018:0320JUD003768510 (scl_judgment_exact_date) over ECLI:CE:ECHR:2016:0628JUD002276812 (date=2016-06-28, branch=None, sec=None); scl='Rad |
| 58 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2000:0919DEC003172596` | `ECLI:CE:ECHR:2000:0404JUD003142396` (2000-04-04, judgment) | v2 chose ECLI:CE:ECHR:1999:0325JUD003142396 (scl_judgment_exact_date) over ECLI:CE:ECHR:2000:0404JUD003142396 (date=2000-04-04, branch=GRANDCHAMBER, sec=just_sa |
| 59 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2024:1022JUD000176623` | `ECLI:CE:ECHR:2022:0317JUD002482714` (2022-03-17, judgment) | v2 chose ECLI:CE:ECHR:2023:0601JUD002482714 (scl_judgment_exact_date) over ECLI:CE:ECHR:2022:0317JUD002482714 (date=2022-03-17, branch=None, sec=None); scl='Fu  |
| 60 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2010:1109JUD002274603` | `ECLI:CE:ECHR:2002:0618JUD004893999` (2002-06-18, judgment) | v2 chose ECLI:CE:ECHR:2004:1130JUD004893999 (scl_judgment_year) over ECLI:CE:ECHR:2002:0618JUD004893999 (date=2002-06-18, branch=None, sec=None); scl='Öneryildi |

*(Showing first 60 of 320 stratified dropped rows; all 320 rows are in `r6_1_echr_edge_v2_gate.json`.)*

## 6. Kept Edge Precision Sample (60 rows, showing first 30)

| # | Source ECLI | Target ECLI (Date, Kind) | Rule | `scl` Reference |
|---:|---|---|---|---|
| 1 | `ECLI:CE:ECHR:2004:1109JUD004198498` | `ECLI:CE:ECHR:2004:0406DEC005691800` (2004-04-06, decision) | `scl_decision_cue` | Leshchenko and Tolyupa v. Ukraine (dec.), no. 56918/00, 6 April 2004 |
| 2 | `ECLI:CE:ECHR:2005:1004JUD001132402` | `ECLI:CE:ECHR:2005:0111JUD007422101` (2005-01-11, judgment) | `scl_judgment` | Dubenko v. Ukraine, no. 74221/01, §§ 22 29, 47 and 51, 30 November 2004 |
| 3 | `ECLI:CE:ECHR:2005:0118DEC000971303` | `ECLI:CE:ECHR:2003:1127DEC000429002` (2003-11-27, decision) | `scl_decision_cue` | Natoli c. Italie (déc.), n° 4290/02, 27 novembre 2003 |
| 4 | `ECLI:CE:ECHR:2021:0511JUD004416615` | `ECLI:CE:ECHR:2011:0324JUD002345802` (2011-03-24, judgment) | `scl_judgment_year` | Giuliani and Gaggio v. Italy [GC], no 23458/02, § 306, ECHR 2011 (extracts) |
| 5 | `ECLI:CE:ECHR:2023:0119JUD003266719` | `ECLI:CE:ECHR:2013:0219JUD000283406` (2013-02-19, judgment) | `scl_judgment` | Petko Petkov v. Bulgaria, no. 2834/06, § 33, 19 February 2013 |
| 6 | `ECLI:CE:ECHR:2023:1121JUD004852319` | `ECLI:CE:ECHR:2020:0114JUD000229715` (2020-01-14, judgment) | `scl_judgment` | S.A. and Others v. Russia [Committee], no. 2297/15, § 63, 14 January 2020 |
| 7 | `ECLI:CE:ECHR:2022:1110JUD000508418` | `ECLI:CE:ECHR:2011:0906DEC004805809` (2011-09-06, decision) | `scl_decision_cue` | Müller v. the Czech Republic (dec.), no. 48058/09, 6 September 2011 |
| 8 | `ECLI:CE:ECHR:2003:1106JUD003444297` | `ECLI:CE:ECHR:1999:0728JUD002277493` (1999-07-28, judgment) | `scl_judgment` | Immobiliare Saffi v. Italy [GC], no. 22774/93, §§ 18-35, §§ 46-75, ECHR 1999-V |
| 9 | `ECLI:CE:ECHR:2023:1005JUD002271612` | `ECLI:CE:ECHR:2022:1206JUD003985914` (2022-12-06, judgment) | `scl_judgment` | Pannon Plakát Kft and Others v. Hungary, no. 39859/14, §§ 44 and 46, 6 December 2022 |
| 10 | `ECLI:CE:ECHR:2025:0116JUD001545720` | `ECLI:CE:ECHR:2022:0203DEC001435619` (2022-02-03, decision) | `scl_decision_cue` | S.M.K. v. France (dec.), no 14356/19, 3 February 2022 |
| 11 | `ECLI:CE:ECHR:2015:1013JUD001132714` | `ECLI:CE:ECHR:2015:0122JUD001519112` (2015-01-22, judgment) | `scl_judgment` | Kitanovski v. the former Yugoslav Republic of Macedonia, no. 15191/12, § 52 to 57, 22 January 2015 |
| 12 | `ECLI:CE:ECHR:2012:0124JUD005754109` | `ECLI:CE:ECHR:2010:0921JUD000739007` (2010-09-21, judgment) | `scl_judgment` | Kevin O’Dowd v. the United Kingdom, no. 7390/07, §§ 71-77, 21 September 2010 |
| 13 | `ECLI:CE:ECHR:2023:0126JUD002238619` | `ECLI:CE:ECHR:2015:0512JUD003686205` (2015-05-12, judgment) | `scl_judgment` | Gogitidze and Others v. Georgia, no. 36862/05, § 96, 12 May 2015 |
| 14 | `ECLI:CE:ECHR:2014:0325JUD003859010` | `ECLI:CE:ECHR:2011:0614JUD003805809` (2011-06-14, judgment) | `scl_judgment` | Osman v. Denmark, no. 38058/09, § 53, 14 June 2011 |
| 15 | `ECLI:CE:ECHR:2014:0311DEC006044113` | `ECLI:CE:ECHR:2010:0603DEC005962308` (2010-06-03, decision) | `scl_decision_cue_exact_date` | Emin and Others v Cyprus, Greece and the United Kingdom, nos. 59623/08 et al, (dec.) 3 June 2010 |
| 16 | `ECLI:CE:ECHR:2021:0914JUD004164319` | `ECLI:CE:ECHR:2005:1027JUD003223102` (2005-10-27, judgment) | `scl_judgment` | Keles v. Germany, no. 32231/02, § 66, 27 October 2005 |
| 17 | `ECLI:CE:ECHR:2006:1107JUD001507202` | `ECLI:CE:ECHR:2006:0530JUD007115201` (2006-05-30, judgment) | `scl_judgment` | Barszcz v. Poland, no. 71152/01, §§ 26-35, §§ 41-45, 30 May 2006 |
| 18 | `ECLI:CE:ECHR:2021:1214JUD002391415` | `ECLI:CE:ECHR:2016:0330JUD000587808` (2016-03-30, judgment) | `scl_judgment` | Armani Da Silva v. the United Kingdom [GC], no. 5878/08, § 243, 30 March 2016 |
| 19 | `ECLI:CE:ECHR:2024:1203JUD002050320` | `ECLI:CE:ECHR:2019:0618JUD001512217` (2019-06-18, judgment) | `scl_judgment` | Vladimir Ushakov v. Russia, no. 15122/17, § 88, 18 June 2019 |
| 20 | `ECLI:CE:ECHR:2014:0708JUD003827011` | `ECLI:CE:ECHR:2006:1003JUD000054303` (2006-10-03, judgment) | `scl_judgment` | McKay v. the United Kingdom [GC], no 543/03, § 43, ECHR 2006 X |
| 21 | `ECLI:CE:ECHR:2019:0409JUD002388716` | `ECLI:CE:ECHR:2016:1108JUD005697110` (2016-11-08, judgment) | `scl_judgment` | El Ghatet v. Switzerland, no 56971/10, §§ 47 and 52, 8 November 2016 |
| 22 | `ECLI:CE:ECHR:2006:0613DEC000751004` | `ECLI:CE:ECHR:2000:0627JUD002227793` (2000-06-27, judgment) | `scl_judgment` | Ilhan v. Turkey [GC], no. 22277/93, §§ 58-59, ECHR 2000 VII |
| 23 | `ECLI:CE:ECHR:2019:0711JUD002850811` | `ECLI:CE:ECHR:2015:1217JUD002485311` (2015-12-17, judgment) | `scl_judgment` | Vugar Aliyev and Others v. Azerbaijan [Committee], nos. 24853/11 and 3 others, 17 December 2015 |
| 24 | `ECLI:CE:ECHR:2001:0906DEC006978901` | `ECLI:CE:ECHR:2001:0522JUD003359296` (2001-05-22, judgment) | `scl_judgment` | Arrêt Baumann c. France, no. 33592/96, § 47, - (22.5.01) |
| 25 | `ECLI:CE:ECHR:2007:0703JUD003100103` | `ECLI:CE:ECHR:2004:1221JUD006151300` (2004-12-21, judgment) | `scl_judgment` | Busuioc v. Moldova (no. 61513/00, § 52-54, 21 December 2004 |
| 26 | `ECLI:CE:ECHR:2018:0201JUD004528512` | `ECLI:CE:ECHR:2009:1203JUD001896703` (2009-12-03, judgment) | `scl_judgment_exact_date` | Mutishev and Others v. Bulgaria, no. 18967/03, § 160, 3 December 2009 |
| 27 | `ECLI:CE:ECHR:2005:1222JUD004634799` | `ECLI:CE:ECHR:2003:0731JUD001616390` (2003-07-31, judgment) | `scl_judgment` | Eugenia Michaelidou Developments Ltd and Michael Tymvios v. Turkey, no. 16163/90, §§ 28-31, 31 July 2003 |
| 28 | `ECLI:CE:ECHR:2022:1122JUD004439415` | `ECLI:CE:ECHR:2020:0211JUD001561310` (2020-02-11, judgment) | `scl_judgment` | Vovk and Bogdanov v. Russia, no. 15613/10, § 75, 11 February 2020 |
| 29 | `ECLI:CE:ECHR:2003:0925DEC006141600` | `ECLI:CE:ECHR:2003:0327DEC005869800` (2003-03-27, decision) | `scl_decision_cue` | Paulino Tomas c. Portugal (déc.), n° 58698/00, CEDH 2003 |
| 30 | `ECLI:CE:ECHR:2002:1001JUD003625597` | `ECLI:CE:ECHR:2002:0723JUD005613200` (2002-07-23, judgment) | `scl_judgment` | Taskin v. Germany judgment of 23 July 2002, application no. 56132/00 |
