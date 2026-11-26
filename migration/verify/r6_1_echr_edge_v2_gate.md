# R6.1 ECtHR Edge v2 Staging Gate Report (`r6.1-v4`)

## 1. Global Parity & Network Metrics (Before vs After)

| Metric | Legacy `echr_edge` | Staged `echr_edge_v2` | Delta |
|---|---:|---:|---:|
| Resolved edges | 428,440 | 157,980 | -270,460 |
| Distinct source cases | 19,129 | 19,304 | +175 |
| Distinct target cases | 44,003 | 18,836 | -25,167 |
| Mean out-degree | 22.40 | 8.18 | -14.22 |
| p50 / p90 / p99 out-degree | 7 / 36 / 91 | 4 / 21 / 51 | — |
| Max out-degree | 15,597 | 205 | -15,392 |
| Cases with >100 outgoing edges | 151 | 14 | -137 |
| Cases with >300 outgoing edges (`case_cited_by` cutoff) | 54 | 0 | -54 |
| Edges trapped in >300 out-degree cases | 164,177 | 0 | -164,177 |

- **Kept:** `155,453`
- **Dropped (total):** `272,987` (`269,060` on sources with `scl`; `3,927` on 148 post-Oct-27 weekly-ETL sources whose `echr_document.scl` is NULL because `PostgresECHRProcessor._detail_row` did not persist `scl`)
- **Newly added (recovered from `scl`):** `2,527`

## 2. Classification of ALL Dropped Legacy Edges

| Drop Reason | Rows | % of Drops | Sampled in Gate |
|---|---:|---:|---:|
| `non_owner_appno_fanout` | 162,214 | 59.42% | 95 |
| `wrong_doctype_decision_without_dec_cue` | 78,735 | 28.84% | 59 |
| `extra_judgment_for_same_appno` | 17,840 | 6.54% | 33 |
| `wrong_doctype_judgment_for_dec_cue` | 4,129 | 1.51% | 27 |
| `source_no_scl` | 3,927 | 1.44% | 0 |
| `future_target_date` | 3,045 | 1.12% | 26 |
| `extra_decision_for_same_appno` | 2,877 | 1.05% | 26 |
| `unmatched_with_owner_appno_in_scl` | 162 | 0.06% | 25 |
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
| `scl_judgment` | 103,921 |
| `scl_decision_cue` | 25,760 |
| `scl_judgment_year` | 13,099 |
| `scl_judgment_exact_date` | 8,805 |
| `scl_decision_cue_exact_date` | 2,639 |
| `scl_decision_no_judgment` | 1,646 |
| `scl_judgment_year_gc_principal_ecli_appno` | 408 |
| `scl_decision_cue_year` | 297 |
| `scl_judgment_year_principal_ecli_appno` | 285 |
| `scl_judgment_exact_date_year_principal_ecli_appno` | 260 |
| `scl_decision_cue_year_principal_earliest` | 126 |
| `scl_decision_cue_principal_earliest` | 111 |
| `scl_judgment_year_principal` | 110 |
| `scl_decision_no_judgment_exact_date` | 71 |
| `scl_judgment_gc_just_satisfaction` | 55 |
| `scl_judgment_principal` | 52 |
| `scl_decision_cue_exact_date_year_principal_earliest` | 51 |
| `scl_decision_no_judgment_principal_earliest` | 50 |
| `scl_judgment_gc` | 45 |
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
| 5 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0131JUD001288802` | `ECLI:CE:ECHR:2004:0921DEC001121502` (2004-09-21, decision) | v2 chose ECLI:CE:ECHR:2005:0531DEC001121502 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2004:0921DEC001121502; scl='(dec), 11215/02, 31 May 2005' |
| 6 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0530DEC003373603` | `ECLI:CE:ECHR:1998:0414DEC002752695` (1998-04-14, decision) | v2 chose ECLI:CE:ECHR:2002:0425DEC002752695 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1998:0414DEC002752695; scl='Günaydin c. Turquie (déc.), no 27526/95, |
| 7 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2020:1013JUD008111417` | `ECLI:CE:ECHR:2012:0306DEC004221907` (2012-03-06, decision) | v2 chose ECLI:CE:ECHR:2015:0709DEC004221907 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2012:0306DEC004221907; scl='Gherghina v. Romania (dec.) [GC], no. 42 |
| 8 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2005:0303DEC005969600` | `ECLI:CE:ECHR:2000:0111DEC003131296` (2000-01-11, decision) | v2 chose ECLI:CE:ECHR:1998:1021DEC003131296 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0111DEC003131296; scl='No. 31312/96, Dec. 21.10.98' |
| 9 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2019:1128JUD000123017` | `ECLI:CE:ECHR:1999:0511DEC003286096` (1999-05-11, decision) | v2 chose ECLI:CE:ECHR:2004:0210DEC003286096 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1999:0511DEC003286096; scl='Önen v. Turkey, (dec.), no 32860/96, 10  |
| 10 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2019:0305JUD003636606` | `ECLI:CE:ECHR:2012:1113DEC004531211` (2012-11-13, decision) | v2 chose ECLI:CE:ECHR:2012:0207DEC004531211 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2012:1113DEC004531211; scl='Frimu and Others v. Romania (dec.), nos. |
| 11 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0207JUD003453902` | `ECLI:CE:ECHR:2000:0706DEC004397898` (2000-07-06, decision) | v2 chose ECLI:CE:ECHR:2003:0930DEC004397898 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0706DEC004397898; scl='Kepa v. Poland (dec.), no. 43978/98, 30  |
| 12 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2002:0917DEC005218999` | `ECLI:CE:ECHR:2002:0305DEC004985799` (2002-03-05, decision) | v2 chose ECLI:CE:ECHR:2001:0125DEC004985799 (scl_decision_cue_principal_earliest) over ECLI:CE:ECHR:2002:0305DEC004985799; scl='Ottomani c. France (déc.), n° 49 |
| 13 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2007:0522JUD004729799` | `ECLI:CE:ECHR:2003:0610DEC006584901` (2003-06-10, decision) | v2 chose ECLI:CE:ECHR:2005:1018DEC006584901 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:0610DEC006584901; scl='Güzel v. Turkey (dec.), no. 65849/01, 18 |
| 14 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:0718JUD007645001` | `ECLI:CE:ECHR:2003:0527DEC006825501` (2003-05-27, decision) | v2 chose ECLI:CE:ECHR:2002:1105DEC006825501 (scl_decision_cue_principal_earliest) over ECLI:CE:ECHR:2003:0527DEC006825501; scl='Crochard and Others v. France (d |
| 15 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2005:0630DEC007807401` | `ECLI:CE:ECHR:2004:0504DEC006411700` (2004-05-04, decision) | v2 chose ECLI:CE:ECHR:2003:0318DEC006411700 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2004:0504DEC006411700; scl='Guala c. France (déc.), n° 64117/00, 18  |
| 16 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2013:0430JUD002578704` | `ECLI:CE:ECHR:2008:0401DEC003360503` (2008-04-01, decision) | v2 chose ECLI:CE:ECHR:2009:0616DEC003360503 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2008:0401DEC003360503; scl='Constantinescu et autres c. Roumanie (dé |
| 17 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2003:0603DEC005183299` | `ECLI:CE:ECHR:1999:0601DEC003866597` (1999-06-01, decision) | v2 chose ECLI:CE:ECHR:2002:0611DEC003866597 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1999:0601DEC003866597; scl='Bukowski v. Poland (dec.), no. 38665/97, |
| 18 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2012:1016DEC005177007` | `ECLI:CE:ECHR:2003:1023DEC002928802` (2003-10-23, decision) | v2 chose ECLI:CE:ECHR:2004:1130DEC002928802 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:1023DEC002928802; scl='Roseiro Bento c. Portugal (déc.), no 292 |
| 19 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2021:0701JUD006642409` | `ECLI:CE:ECHR:2004:1026DEC002857803` (2004-10-26, decision) | v2 chose ECLI:CE:ECHR:2006:0627DEC002857803 (scl_decision_cue_year) over ECLI:CE:ECHR:2004:1026DEC002857803; scl='Szabó v. Sweden (dec.), no. 28578/03, ECHR 200 |
| 20 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2014:1014JUD004544004` | `ECLI:CE:ECHR:2002:0326DEC003480197` (2002-03-26, decision) | v2 chose ECLI:CE:ECHR:2000:0518DEC004500398 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2002:0326DEC003480197; scl='Valentini v. Italy (dec.), no 45003/98,  |
| 21 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2007:0412JUD006645501` | `ECLI:CE:ECHR:2003:0610DEC004978199` (2003-06-10, decision) | v2 chose ECLI:CE:ECHR:2004:0629DEC004978199 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:0610DEC004978199; scl='Florica v. Romania (dec.), no. 49781/99, |
| 22 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:1214JUD000119804` | `ECLI:CE:ECHR:2003:1216DEC002943902` (2003-12-16, decision) | v2 chose ECLI:CE:ECHR:2002:1126DEC002943902 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:1216DEC002943902; scl='Sokur v. Ukraine (dec.), no. 29439/02, 2 |
| 23 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2025:0520JUD004424120` | `ECLI:CE:ECHR:2000:0406DEC005084199` (2000-04-06, decision) | v2 chose ECLI:CE:ECHR:2001:1011DEC005084199 (scl_decision_cue_year) over ECLI:CE:ECHR:2000:0406DEC005084199; scl='Osmani and Others v. the former Yugoslav Repub |
| 24 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:1010JUD006588801` | `ECLI:CE:ECHR:2000:0706DEC004800199` (2000-07-06, decision) | v2 chose ECLI:CE:ECHR:2001:1023DEC004800199 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0706DEC004800199; scl='Goc v. Poland (dec.), no. 48001/99, 23 O |
| 25 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2006:1017JUD000541302` | `ECLI:CE:ECHR:2004:0921DEC001121502` (2004-09-21, decision) | v2 chose ECLI:CE:ECHR:2005:0531DEC001121502 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2004:0921DEC001121502; scl='Ratajczyk v. Poland (dec.), no. 11215/02 |
| 26 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2004:1207DEC007107401` | `ECLI:CE:ECHR:1965:0715DEC000233364` (1965-07-15, decision) | v2 chose ECLI:CE:ECHR:1968:1216DEC000233364 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:1965:0715DEC000233364; scl='Eur. Comm. H.R. No 2333/64, dec. 16.12.6 |
| 27 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2011:0318JUD003081406` | `ECLI:CE:ECHR:2006:0509DEC004625499` (2006-05-09, decision) | v2 chose ECLI:CE:ECHR:2004:1130DEC004625499 (scl_decision_cue_principal_earliest) over ECLI:CE:ECHR:2006:0509DEC004625499; scl='Bulski v. Poland (dec.), nos. 46 |
| 28 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2001:1206DEC003117896` | `ECLI:CE:ECHR:2000:0307DEC004503598` (2000-03-07, decision) | v2 chose ECLI:CE:ECHR:2001:0925DEC004503598 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2000:0307DEC004503598; scl='K.S. v. the United Kingdom (dec.), no. 4 |
| 29 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2015:0312JUD004323104` | `ECLI:CE:ECHR:2003:0904DEC007502501` (2003-09-04, decision) | v2 chose ECLI:CE:ECHR:2006:0323DEC007502501 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:0904DEC007502501; scl='Aleksentseva and 28 Others v. Russia (de |
| 30 | `extra_decision_for_same_appno` | `ECLI:CE:ECHR:2005:0524JUD007104701` | `ECLI:CE:ECHR:2003:0318DEC006113900` (2003-03-18, decision) | v2 chose ECLI:CE:ECHR:2004:1118DEC006113900 (scl_decision_cue_exact_date) over ECLI:CE:ECHR:2003:0318DEC006113900; scl='Le Duigou v. France (dec.), no. 61139/00 |
| 31 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2009:0416JUD004900006` | `ECLI:CE:ECHR:2000:0404JUD003142396` (2000-04-04, judgment) | v2 chose ECLI:CE:ECHR:1999:0325JUD003142396 (scl_judgment_year) over ECLI:CE:ECHR:2000:0404JUD003142396 (date=2000-04-04, branch=GRANDCHAMBER, sec=just_satisfac |
| 32 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:1008JUD008011717` | `ECLI:CE:ECHR:2001:1011JUD003094396` (2001-10-11, judgment) | v2 chose ECLI:CE:ECHR:2003:0708JUD003094396 (scl_judgment_year) over ECLI:CE:ECHR:2001:1011JUD003094396 (date=2001-10-11, branch=None, sec=None); scl='Sahin v.  |
| 33 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2021:1123JUD004044417` | `ECLI:CE:ECHR:2011:1018JUD000907407` (2011-10-18, judgment) | v2 chose ECLI:CE:ECHR:2010:0323JUD000907407 (scl_judgment_exact_date) over ECLI:CE:ECHR:2011:1018JUD000907407 (date=2011-10-18, branch=None, sec=striking_out);  |
| 34 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2016:0426JUD001051110` | `ECLI:CE:ECHR:2008:0630JUD002297805` (2008-06-30, judgment) | v2 chose ECLI:CE:ECHR:2010:0601JUD002297805 (scl_judgment_year) over ECLI:CE:ECHR:2008:0630JUD002297805 (date=2008-06-30, branch=None, sec=None); scl='Gäfgen v. |
| 35 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2025:1023JUD001680321` | `ECLI:CE:ECHR:2012:0828JUD001715311` (2012-08-28, judgment) | v2 chose ECLI:CE:ECHR:2014:0325JUD001715311 (scl_judgment_exact_date) over ECLI:CE:ECHR:2012:0828JUD001715311 (date=2012-08-28, branch=None, sec=None); scl='Vuč |
| 36 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2021:0121JUD001536714` | `ECLI:CE:ECHR:2015:0901JUD001648312` (2015-09-01, judgment) | v2 chose ECLI:CE:ECHR:2016:1215JUD001648312 (scl_judgment_exact_date) over ECLI:CE:ECHR:2015:0901JUD001648312 (date=2015-09-01, branch=None, sec=None); scl='Khl |
| 37 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2016:0614JUD003521409` | `ECLI:CE:ECHR:2000:0725JUD003398596` (2000-07-25, judgment) | v2 chose ECLI:CE:ECHR:1999:0927JUD003398596 (scl_judgment_year) over ECLI:CE:ECHR:2000:0725JUD003398596 (date=2000-07-25, branch=None, sec=just_satisfaction); s |
| 38 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2005:1117JUD006324100` | `ECLI:CE:ECHR:2001:0123JUD002834295` (2001-01-23, judgment) | v2 chose ECLI:CE:ECHR:1999:1028JUD002834295 (scl_judgment_year) over ECLI:CE:ECHR:2001:0123JUD002834295 (date=2001-01-23, branch=GRANDCHAMBER, sec=just_satisfac |
| 39 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2022:0621JUD001042519` | `ECLI:CE:ECHR:2013:0628JUD001657408` (2013-06-28, judgment) | v2 chose ECLI:CE:ECHR:2013:0207JUD001657408 (scl_judgment_year_gc_principal) over ECLI:CE:ECHR:2013:0628JUD001657408 (date=2013-06-28, branch=GRANDCHAMBER, sec= |
| 40 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2018:0116JUD002741309` | `ECLI:CE:ECHR:2010:0114JUD005452200` (2010-01-14, judgment) | v2 chose ECLI:CE:ECHR:2012:0403JUD005452200 (scl_judgment_exact_date) over ECLI:CE:ECHR:2010:0114JUD005452200 (date=2010-01-14, branch=None, sec=None); scl='Kot |
| 41 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2005:0715JUD006324200` | `ECLI:CE:ECHR:2001:0123JUD002834295` (2001-01-23, judgment) | v2 chose ECLI:CE:ECHR:1999:1028JUD002834295 (scl_judgment_year) over ECLI:CE:ECHR:2001:0123JUD002834295 (date=2001-01-23, branch=GRANDCHAMBER, sec=just_satisfac |
| 42 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2012:0626JUD002682806` | `ECLI:CE:ECHR:2006:0622JUD005964300` (2006-06-22, judgment) | v2 chose ECLI:CE:ECHR:2007:1207JUD005964300 (scl_judgment_exact_date) over ECLI:CE:ECHR:2006:0622JUD005964300 (date=2006-06-22, branch=None, sec=None); scl='Kaf |
| 43 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:1003JUD001487920` | `ECLI:CE:ECHR:2022:0623JUD001975013` (2022-06-23, judgment) | v2 chose ECLI:CE:ECHR:2023:0601JUD001975013 (scl_judgment_exact_date) over ECLI:CE:ECHR:2022:0623JUD001975013 (date=2022-06-23, branch=None, sec=None); scl='Gro |
| 44 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:0213JUD000867515` | `ECLI:CE:ECHR:2015:0901JUD001648312` (2015-09-01, judgment) | v2 chose ECLI:CE:ECHR:2016:1215JUD001648312 (scl_judgment_exact_date) over ECLI:CE:ECHR:2015:0901JUD001648312 (date=2015-09-01, branch=None, sec=None); scl='Khl |
| 45 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2012:1018JUD003767908` | `ECLI:CE:ECHR:2001:0504JUD002474694` (2001-05-04, judgment) | v2 chose ECLI:CE:ECHR:2001:0504JUD003005496 (scl_judgment_exact_date_year_principal_ecli_appno) over ECLI:CE:ECHR:2001:0504JUD002474694 (date=2001-05-04, branch |
| 46 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2010:0928JUD004258306` | `ECLI:CE:ECHR:2005:1208JUD005885800` (2005-12-08, judgment) | v2 chose ECLI:CE:ECHR:2009:1222JUD005885800 (scl_judgment_exact_date) over ECLI:CE:ECHR:2005:1208JUD005885800 (date=2005-12-08, branch=None, sec=None); scl='Gui |
| 47 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2010:1102JUD003815502` | `ECLI:CE:ECHR:2005:0928JUD003144396` (2005-09-28, judgment) | v2 chose ECLI:CE:ECHR:2004:0622JUD003144396 (scl_judgment_year) over ECLI:CE:ECHR:2005:0928JUD003144396 (date=2005-09-28, branch=GRANDCHAMBER, sec=striking_out) |
| 48 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2014:0325JUD001715311` | `ECLI:CE:ECHR:2000:0427JUD002570294` (2000-04-27, judgment) | v2 chose ECLI:CE:ECHR:2001:0712JUD002570294 (scl_judgment_year) over ECLI:CE:ECHR:2000:0427JUD002570294 (date=2000-04-27, branch=None, sec=None); scl='K. and T. |
| 49 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:0505DEC000359918` | `ECLI:CE:ECHR:2008:0710JUD000339403` (2008-07-10, judgment) | v2 chose ECLI:CE:ECHR:2010:0329JUD000339403 (scl_judgment_year) over ECLI:CE:ECHR:2008:0710JUD000339403 (date=2008-07-10, branch=None, sec=None); scl='Medvedyev |
| 50 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:1121DEC002524020` | `ECLI:CE:ECHR:2016:0614JUD007250813` (2016-06-14, judgment) | v2 chose ECLI:CE:ECHR:2017:1128JUD007250813 (scl_judgment_exact_date) over ECLI:CE:ECHR:2016:0614JUD007250813 (date=2016-06-14, branch=None, sec=None); scl='Mer |
| 51 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2020:0204JUD006085815` | `ECLI:CE:ECHR:2012:1113JUD000445510` (2012-11-13, judgment) | v2 chose ECLI:CE:ECHR:2014:0527JUD000445510 (scl_judgment_year) over ECLI:CE:ECHR:2012:1113JUD000445510 (date=2012-11-13, branch=None, sec=None); scl='Marguš v. |
| 52 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2022:0111JUD007007812` | `ECLI:CE:ECHR:2018:0619JUD003525208` (2018-06-19, judgment) | v2 chose ECLI:CE:ECHR:2021:0525JUD003525208 (scl_judgment_exact_date) over ECLI:CE:ECHR:2018:0619JUD003525208 (date=2018-06-19, branch=None, sec=None); scl='Cen |
| 53 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:0706JUD002118119` | `ECLI:CE:ECHR:2016:0621JUD005539113` (2016-06-21, judgment) | v2 chose ECLI:CE:ECHR:2018:1106JUD005539113 (scl_judgment_exact_date) over ECLI:CE:ECHR:2016:0621JUD005539113 (date=2016-06-21, branch=None, sec=None); scl='Ram |
| 54 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2022:1122JUD004439415` | `ECLI:CE:ECHR:2005:0928JUD003144396` (2005-09-28, judgment) | v2 chose ECLI:CE:ECHR:2004:0622JUD003144396 (scl_judgment_year) over ECLI:CE:ECHR:2005:0928JUD003144396 (date=2005-09-28, branch=GRANDCHAMBER, sec=striking_out) |
| 55 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2008:0722JUD003578503` | `ECLI:CE:ECHR:2003:0107JUD004491298` (2003-01-07, judgment) | v2 chose ECLI:CE:ECHR:2004:0928JUD004491298 (scl_judgment_year) over ECLI:CE:ECHR:2003:0107JUD004491298 (date=2003-01-07, branch=None, sec=None); scl='Kopecký v |
| 56 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2023:0119JUD003224519` | `ECLI:CE:ECHR:2016:0614JUD007250813` (2016-06-14, judgment) | v2 chose ECLI:CE:ECHR:2017:1128JUD007250813 (scl_judgment_exact_date) over ECLI:CE:ECHR:2016:0614JUD007250813 (date=2016-06-14, branch=None, sec=None); scl='Mer |
| 57 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2025:1023JUD002633819` | `ECLI:CE:ECHR:2022:0623JUD001975013` (2022-06-23, judgment) | v2 chose ECLI:CE:ECHR:2023:0601JUD001975013 (scl_judgment_exact_date) over ECLI:CE:ECHR:2022:0623JUD001975013 (date=2022-06-23, branch=None, sec=None); scl='Gro |
| 58 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2000:0704DEC005171799` | `ECLI:CE:ECHR:2000:0525JUD003836697` (2000-05-25, judgment) | v2 chose ECLI:CE:ECHR:2000:0125JUD003836697 (scl_judgment_exact_date) over ECLI:CE:ECHR:2000:0525JUD003836697 (date=2000-05-25, branch=None, sec=just_satisfacti |
| 59 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2024:1022JUD000176623` | `ECLI:CE:ECHR:2019:0312JUD002637418` (2019-03-12, judgment) | v2 chose ECLI:CE:ECHR:2020:1201JUD002637418 (scl_judgment_exact_date) over ECLI:CE:ECHR:2019:0312JUD002637418 (date=2019-03-12, branch=None, sec=None); scl='Guð |
| 60 | `extra_judgment_for_same_appno` | `ECLI:CE:ECHR:2010:1109JUD000066406` | `ECLI:CE:ECHR:2007:1004JUD003277202` (2007-10-04, judgment) | v2 chose ECLI:CE:ECHR:2009:0630JUD003277202 (scl_judgment_year) over ECLI:CE:ECHR:2007:1004JUD003277202 (date=2007-10-04, branch=None, sec=None); scl='Verein ge |

*(Showing first 60 of 320 stratified dropped rows; all 320 rows are in `r6_1_echr_edge_v2_gate.json`.)*

## 6. Kept Edge Precision Sample (60 rows, showing first 30)

| # | Source ECLI | Target ECLI (Date, Kind) | Rule | `scl` Reference |
|---:|---|---|---|---|
| 1 | `ECLI:CE:ECHR:2004:1109JUD004007798` | `ECLI:CE:ECHR:2004:0406JUD002698295` (2004-04-06, judgment) | `scl_judgment` | Mehdi Zana v. Turkey, no. 26982/95, § 21, 6 April 2004 |
| 2 | `ECLI:CE:ECHR:2005:1004JUD001132402` | `ECLI:CE:ECHR:2004:0629JUD001896602` (2004-06-29, judgment) | `scl_judgment` | Voytenko v. Ukraine, no. 18966/02, §§ 28-31, 29 June 2004 |
| 3 | `ECLI:CE:ECHR:2005:0118DEC000971303` | `ECLI:CE:ECHR:2000:0512JUD003539497` (2000-05-12, judgment) | `scl_judgment` | Khan c. Royaume-Uni, no 35394/97, § 34, CEDH 2000 V |
| 4 | `ECLI:CE:ECHR:2021:0511JUD004416615` | `ECLI:CE:ECHR:2004:0212JUD004728799` (2004-02-12, judgment) | `scl_judgment` | Perez v. France [GC], no 47287/99, § 70, ECHR 2004-I |
| 5 | `ECLI:CE:ECHR:2023:0119JUD003266719` | `ECLI:CE:ECHR:2012:0214DEC002453710` (2012-02-14, decision) | `scl_decision_cue` | Şen and Others v. Turkey (dec.), no. 24537/10, 14 February 2012 |
| 6 | `ECLI:CE:ECHR:2023:1121JUD004852319` | `ECLI:CE:ECHR:2011:0303JUD003348804` (2011-03-03, judgment) | `scl_judgment` | Khambulatova v. Russia, no. 33488/04, §§ 108-09, 3 March 2011 |
| 7 | `ECLI:CE:ECHR:2022:1110JUD000508418` | `ECLI:CE:ECHR:2002:0514DEC003862197` (2002-05-14, decision) | `scl_decision_cue` | Zehnalová and Zehnal v. the Czech Republic (dec.), no. 38621/97, ECHR 2002 V |
| 8 | `ECLI:CE:ECHR:2003:1106DEC006334300` | `ECLI:CE:ECHR:2000:0316DEC003391696` (2000-03-16, decision) | `scl_decision_cue` | Walden v. Liechtenstein (dec.), no. 33916/96, 16 March 2000 |
| 9 | `ECLI:CE:ECHR:2023:1005JUD002271612` | `ECLI:CE:ECHR:2011:0920JUD001490204` (2011-09-20, judgment) | `scl_judgment_exact_date` | OAO Neftyanaya Kompaniya Yukos v. Russia, no. 14902/04, § 559, 20 September 2011 |
| 10 | `ECLI:CE:ECHR:2025:0116JUD001545720` | `ECLI:CE:ECHR:2014:0717JUD003254108` (2014-07-17, judgment) | `scl_judgment_exact_date` | Svinarenko and Slyadnev v. Russia [GC], nos. 32541/08 and 43441/08, § 114, 17 July 2014 |
| 11 | `ECLI:CE:ECHR:2015:1013JUD001132714` | `ECLI:CE:ECHR:2000:0627JUD002227793` (2000-06-27, judgment) | `scl_judgment` | İlhan v. Turkey [GC], no. 22277/93, § 75, ECHR 2000-VII |
| 12 | `ECLI:CE:ECHR:2012:0124JUD005754109` | `ECLI:CE:ECHR:2009:0730JUD003439303` (2009-07-30, judgment) | `scl_judgment` | Pitalev v. Russia, no. 34393/03, § 54, 30 July 2009 |
| 13 | `ECLI:CE:ECHR:2023:0126JUD002238619` | `ECLI:CE:ECHR:2012:0619JUD001776708` (2012-06-19, judgment) | `scl_judgment` | Khoniakina v. Georgia, no. 17767/08, 19 June 2012 |
| 14 | `ECLI:CE:ECHR:2014:0325JUD003859010` | `ECLI:CE:ECHR:2007:1113JUD005732500` (2007-11-13, judgment) | `scl_judgment_year` | D.H. and Others v. the Czech Republic [GC], no. 57325/00, ECHR 2007 |
| 15 | `ECLI:CE:ECHR:2014:0311DEC006044113` | `ECLI:CE:ECHR:2005:0519DEC002807904` (2005-05-19, decision) | `scl_decision_cue` | Green v. the United Kingdom, no. 28079/04, (dec.) 19 May 2005 |
| 16 | `ECLI:CE:ECHR:2021:0914JUD004041919` | `ECLI:CE:ECHR:2019:0709JUD004126117` (2019-07-09, judgment) | `scl_judgment` | Volodina v. Russia, no. 41261/17, 9 July 2019 |
| 17 | `ECLI:CE:ECHR:2006:1107JUD001269703` | `ECLI:CE:ECHR:2005:0215JUD006841601` (2005-02-15, judgment) | `scl_judgment` | Steel and Morris v. the United Kingdom, judgment of 15 February 2005, no. 68416/01, ECHR 2005-II, §§ 88-89 |
| 18 | `ECLI:CE:ECHR:2021:1214JUD002391415` | `ECLI:CE:ECHR:2013:0611DEC006554212` (2013-06-11, decision) | `scl_decision_cue` | Stichting Mothers of Srebrenica and Others v. the Netherlands (dec.), no. 65542/12, §§ 115-116, ECHR 2013 (extracts) |
| 19 | `ECLI:CE:ECHR:2024:1203JUD002050320` | `ECLI:CE:ECHR:2013:0801JUD004772110` (2013-08-01, judgment) | `scl_judgment` | Antonyuk v. Russia, no. 47721/10, § 134, 1 August 2013 |
| 20 | `ECLI:CE:ECHR:2014:0708JUD003827011` | `ECLI:CE:ECHR:2005:0512JUD004622199` (2005-05-12, judgment) | `scl_judgment_year` | Öcalan v. Turkey [GC], no 46221/99, § 181, ECHR 2005 IV |
| 21 | `ECLI:CE:ECHR:2019:0409JUD002388716` | `ECLI:CE:ECHR:2012:0912JUD001059308` (2012-09-12, judgment) | `scl_judgment` | Nada v. Switzerland [GC], no 10593/08, § 164, ECHR 2012 |
| 22 | `ECLI:CE:ECHR:2006:0612DEC003204503` | `ECLI:CE:ECHR:2004:0217DEC004178502` (2004-02-17, decision) | `scl_decision_cue` | Hernández Cairós c. Espagne (déc.), no 41785/02, 17 février 2004 |
| 23 | `ECLI:CE:ECHR:2019:0711JUD002850811` | `ECLI:CE:ECHR:2012:0522JUD000012605` (2012-05-22, judgment) | `scl_judgment_exact_date` | Scoppola v. Italy (no. 3) [GC], no. 126/05, § 84, 22 May 2012 |
| 24 | `ECLI:CE:ECHR:2001:0906DEC006978901` | `ECLI:CE:ECHR:1999:0728JUD002580394` (1999-07-28, judgment) | `scl_judgment` | Arrêt Selmouni c. France [GC], n° 25803/94, §§ 74, 75, CEDH 1999-V - (28.7.99) |
| 25 | `ECLI:CE:ECHR:2007:0629JUD001580902` | `ECLI:CE:ECHR:2006:0711JUD005481000` (2006-07-11, judgment) | `scl_judgment` | Jalloh v. Germany [GC], no. 54810/00, ECHR 2006, §§ 113-116 |
| 26 | `ECLI:CE:ECHR:2018:0201JUD000937315` | `ECLI:CE:ECHR:2016:0323JUD004361111` (2016-03-23, judgment) | `scl_judgment_exact_date` | F.G. v. Sweden [GC], no. 43611/11, 23 March 2016, ECHR 2016 |
| 27 | `ECLI:CE:ECHR:2005:1222JUD004053098` | `ECLI:CE:ECHR:2000:0321JUD004011198` (2000-03-21, judgment) | `scl_judgment` | Gergouil v. France, no. 40111/98, § 19, 21 March 2000 |
| 28 | `ECLI:CE:ECHR:2022:1122JUD004439415` | `ECLI:CE:ECHR:2014:0717JUD004784808` (2014-07-17, judgment) | `scl_judgment` | Centre for Legal Resources on behalf of Valentin Câmpeanu v. Romania [GC], no. 47848/08, § 131, ECHR 2014 |
| 29 | `ECLI:CE:ECHR:2003:0925DEC006089800` | `ECLI:CE:ECHR:2003:0327DEC006530501` (2003-03-27, decision) | `scl_decision_cue` | Gouveia da Silva Torrado c. Portugal (déc.), n° 65305/01, CEDH 2003 |
| 30 | `ECLI:CE:ECHR:2002:1001JUD003362796` | `ECLI:CE:ECHR:1997:0320JUD002191593` (1997-03-20, judgment) | `scl_judgment` | Loukanov v. Bulgaria, no. 21915/93, § 53, 20.03.1997 |
