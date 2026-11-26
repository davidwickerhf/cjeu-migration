# Research evidence, 2026-11-24

Working files behind `docs/CROSS_DATASET_ROADMAP.md`. Everything here was produced
read-only against production `cle_v2` through the sql-runner (results capped at 1,000
rows per query; a literal percent sign breaks it, so the SQL uses `left()`,
`position()` and `~`). Scripts import `runner_q` from the operator's machine; on another
host, point them at your own read-only query helper. The texts and identifiers are public
case law; there are no credentials in this folder.

## goldset/

- `data.js`: the E-01 answer key (from the test.wicker.life prototype): 22 documents and
  20 links around Tjebbes (C-221/17, ECLI:EU:C:2019:189) and X, Y and Z (C-199/12 to
  C-201/12, ECLI:EU:C:2013:720), grouped as "Referred by", "Decided after the ruling",
  "Applies the ruling" and "Strasbourg case law on the same question".
- `trace.py` / `trace.json`: for each link, whether both cases exist, which edges exist in
  either direction, and identifier mentions in `fulltext`.
- `trace2.py` / `trace2.json`: the same with every reference form (ECLI, short ECLI, case
  number incl. joined members and "C 199/12", case name, date, application numbers), and
  AG-opinion checks.

Result: 4 of 20 exist (RVS:2020:423, HR:2020:593 → Tjebbes; RVS:2015:2170, RVS:2015:4062 →
X, Y and Z, all `rs_body_cite`). 17 are citations. HR:2022:637 cites C-221/17 and
EU:C:2019:189 only in `case_text.summary` (the trace scripts searched `fulltext` only;
an independent review caught this). Ramadan, K2 and Usmanov do not cite Tjebbes and are not
cited by it (Ramadan is cited by the referral RVS:2017:1098). F. v. UK (2004) is cited by AG
Sharpston's opinion in C-199/12 (62012CC0199) by application number 17341/03 and
"F. c. Royaume-Uni", not by the judgment. The referrals (RVS:2017:1098, BW3076–BW3078) are
named in neither text; Cellar's `cjeu_document.national_judgement_xml` lists them
(`*A9* Raad van State, 19/04/2017 (201504577/1/A3 ...)`; for X, Y and Z per joined case,
`201012342/T1/V2` etc.). RVS:2013:2422–2424 cite C-199/12 (2422 also C-201/12), the name
and the date, but no ECLI. M.E. v. Sweden, B and C v. Switzerland and I.K. v. Switzerland
(French, "C 199/12" with a plain space) cite the X, Y and Z case numbers.

## rs/ (Rechtspraak side)

Sample: `case_text TABLESAMPLE SYSTEM(1) REPEATABLE(7)`, 9,513 RS texts of 957,843.
`cn.sql` (case numbers), `appno.sql` (application numbers), `resolve.py`, mention and
resolution lists.

- Existing cross-court edges come only from the one-off migration
  (`migration/sql/40_citations.sql`, copying `legacy.rs_edge`): `rs_body_cite` (exact
  `ECLI:` strings, from 2014) and `rs_legacy_ddb` (case and application numbers, frozen in
  2021). Nothing in case-law-explorer `airflow/dags` scans RS text. The 96,517 RS cases
  loaded since the migration have only `rs_lido_sqlite` edges, whose
  `is_cross_jurisdiction` is never set (`row_processors/postgres.py` ~L263–281; default
  false in `clients/postgres.py` ~L216): 6,114 RS→CJEU and 1,427 RS→ECtHR mislabelled.
- Reference forms in the sample: full ECLI:EU in 290 texts (98% already edged); short
  `EU:C:...` 18 texts; case numbers C-/T-/F- 274 texts (824 mentions, 512 edged); "zaak 26/62"
  without prefix 13; court + date only ~48 (beware the Caribbean "Gemeenschappelijk Hof van
  Justitie").
- Case-number rule: match `cjeu_document.case_number`, dated on or before the RS decision;
  44 hand-checked, 42 correct (the 2 misses were source typos next to an ECLI of another
  case). Independent review: 24/25 on its own sample (C-178/98 written for C-178/94); and a
  blind 80-character ECLI rule wrongly drops RBGEL:2026:1195 (C-684/16 sits 65 characters
  from the ECLI of C-619/16), so skip only when the nearby ECLI is the same case number.
  4,864 case numbers have only orders (CO), so fall back to orders. 82 unresolved numbers in
  the sample are mostly non-lead joined members (C-200/12, C-9/90) → needs the joined map
  (Cellar `joins`, ~6.7k members; CJEU texts also state "gevoegde zaken C‑199/12 tot en met
  C‑201/12" with U+2011 hyphens).
- Application numbers (texts mentioning EHRM/EVRM): 397 mentions, 340 resolve via
  `echr_document_appno` `source='appno'`, 175 edged; 24/24 genuine. Single-target rule:
  owner `appno` only (never `extractedappno`), prefer HEJUD/HFJUD, decision only if
  "(dec.)" or no judgment, then cited date, "ECHR YYYY"/[GC] year, earliest judgment before
  the RS decision; correct in all 7 multi-judgment cases.
- Formal relations: `prejudicielevraag` 207 rows to ECLI:EU (124 judgment, 82 AG opinion,
  1 unresolved); the 204 internal ones are the Dutch art. 392 Rv procedure. Opinion → judgment
  by CELEX CC→CJ: 85 of 86. `rechtspraak_extractor` 1.6.0 maps `dcterms:relation` to
  `citations_outgoing` and drops `psi:type` (`rechtspraak_metadata.py` ~L77, L193–210);
  values joined with "\n" but split on "; " (`row_processors/postgres.py` ~L54–57), storing
  labels like "Eerste aanleg: ..." in `target_ecli_raw` (1,192 rows).
- `rs_document_external_authority` (6,046 rows) holds legislation, not cases.

## cjeu/ (CJEU / Cellar side)

`parse_match.py` (parses `national_judgement_xml`), `entries.json`, `matches.json`,
`resolved.json`, `sparql*.py` + `uuid_*.json` (dossier UUID → identifier via public CELLAR
SPARQL), `echr.py` (CJEU text → ECtHR ECLIs).

- Unresolved cellar `cites` (244,151) are almost all non-case-law CELEX: sector 3
  legislation 127,681; 1 treaties 106,308; 5 preparatory 5,423; 2 international agreements
  2,828; 4 1,466; case law only 124. Unresolved `cited_by` 37,873 (sector 5 14,064; 3 10,431;
  8 national decisions 3,912; 9 2,076; E/EFTA 697; OJ notices 6,469; real decisions 158).
  Real case-law misses: 212 rows, 12 CELEX (suffixes `_EXT/_INF/_RES`; CELEX dropped by
  `cjeu_loader.py` `_drop_celex` ~L244–276 on 1,279 cases; `(01)` variants; 2 AG opinions not
  loaded: 62021CC0223, 62021CC0515).
- AG opinions: `cases` rows (CELEX 6YYYYCCnnnn) + `cjeu_ag_opinion` (11,467);
  `parent_case_id` set on 51 (32 point to themselves) because `cjeu_loader.py` `SQL_AG`
  (~L121–136) parses only the rarely-filled joined-to field. CC→CJ swap matches 11,021 of
  11,446 (96.3%), CC→CO 103 more; 20/20 checked.
- `cjeu_national_document` (1,847 rows) are sector-8 national decisions loaded as cases, not
  referral data. Referral data is `cjeu_document.national_judgement_xml` (10,583 docs; 1,119
  mention Dutch courts): A9 referring, A8/A7 earlier instances, P1... follow-ups, I interim.
  Matching on date + zaaknummer in `rs_document` + court: 634 unique links (448 A, 176 P,
  10 I), 70 ambiguous (HR vs PHR same date; duplicate publications such as
  RVS:2012:BW3076 = RVS:2012:4057 = 4059 ENG); a date + court fallback adds ~126 (precision
  not measured). Filter foreign courts on the court prefix only (`parse_match.py` L17 checks
  the whole line).
- Zero-resolved relations: raw URIs passed through (`cjeu_mapping.py` ~L342; exact-CELEX
  joins in `cjeu_loader.py` ~L180–191, L212–219; `cellar_uri`/`work_uri` never filled).
  `subject_to_appeal` 4,038 → 3,348 resolvable via SPARQL `cdm:dossier_identifier`;
  `referred_for_preliminary_ruling` 29 → 17; `joins` 7,515 → best as an alias table;
  `logical_successor_of` 122 noise; `reexamined_by` 5 → 5.
- CJEU → ECtHR: 2,256 English texts sampled; 46 contain `CE:ECHR` ECLIs, 179 distinct, all
  resolve; 20/20 correct; extrapolated ~2,900 edges (±30%). `cjeu_loader.py` ~L183
  hard-codes `is_cross_jurisdiction=false`.

## echr/ (ECtHR side)

`edges.json`, `handcheck.txt`, `nl_refs.txt`, `full_*.txt` (per-batch scan output), `norm.py`.

- HUDOC full text: `case_text` (`source='HUDOC'`), 133,618 non-empty rows covering 101,395
  of 102,315 ECtHR cases (99.1%); other languages in `echr_document_secondary_text`.
- All texts scanned (10 batches by `mod(case_id,10)`): 659 cases contain a CJEU identifier;
  after resolution 1,950 distinct links from 657 sources to 1,006 CJEU documents; 142 case
  numbers unresolved (mostly non-lead joined members). 19/22 exact (the 3 misses picked an AG
  opinion or judgment where an ECLI in the same citation named the right document).
- ECtHR → Dutch: 101 sources, 230 ECLI pairs (173/176 resolve) + ~69 LJN pairs (all 51 LJNs
  match `rs_document.replaces_identifier`, mapping to 66 documents); 5/5 correct.
- Only ECHR→ECHR edges exist because echr-extractor 1.3.5
  (`ECHR_nodes_edges_list_transform.py` L315–319 reads only the HUDOC `scl` field; L244–263
  index only HUDOC documents), `echr_extraction.py` L108–122, `citation_graph_loader.py`
  L27/L30–36/L58–71.
- #69 fan-out: 428,581 rows from 19,124 sources; 58 sources with >200 links hold 165,225
  rows of which 3,329 are supported by an owned application number in `scl`; 78,882
  decisions targeted without "(dec.)" while the judgment is also targeted (12/12 sampled
  cite the judgment); 4,516 judgments where only "(dec.)" was cited; up to 22,247 extra
  judgments per number. About 245,000 confidently wrong (57%); independently confirmed.

## Independent review (Gemini 4 Neon, 2026-11-24)

Confirmed: 4/20 gold links; ~245k wrong ECtHR edges; 634 referral links; RS case-number
yield (~19.9k upper bound). Corrected: HR:2022:637 is a citation (summary);
`rs_lido_sqlite` 81,796 law refs of which 81,776 exactly match `legislation.identifier`
but the loader never sets `legislation_id`; 786,746 of the 786,846 `rs_lido-both` rows
without `legislation_id` resolve through `provision_id → legal_provision`; doubled CJEU
counts come from `41_counts` running before `90_post_load` deduplicated `case_citation`
(migration_manifest: 06:02 vs 08:59) plus the trigger; `replaced_by` cross-court aliases
26,462; self-loops 419. Design changes are in the roadmap's review log.
