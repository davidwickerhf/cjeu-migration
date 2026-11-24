# Citation treatment classification: design

Status: design only (roadmap R5.4, `docs/CROSS_DATASET_ROADMAP.md` on the
roadmap branch). Nothing here is built, trained or run, and no GPU has been
rented. Every compute and volume figure below is an estimate to be replaced by
a measurement in the pilot (step 2 of "Plan").

## Goal

For each occurrence of a cited decision in a judgment (one
`case_citation_mention` row, schema `0009`), say how the citing court treats
it:

- does it rely on it (follows / applies),
- set it aside on the facts (distinguishes),
- or reject or abandon it (departs from),

so the Explorer can show "followed 40 times, distinguished 3 times, departed
from once" instead of a bare citation count, and researchers can filter on
treatment. Classified per mention, not per citation pair: the same judgment
can apply a case in one paragraph and distinguish it in another.

## Labels

| Label | Meaning | Typical cues (NL / EN / FR) |
|---|---|---|
| `follows` | Relies on the cited decision as authority for a rule or interpretation and adopts it | "onder verwijzing naar", "zoals het Hof heeft geoordeeld in", "vaste rechtspraak"; "as the Court held in", "it follows from the case-law"; "selon une jurisprudence constante", "ainsi que la Cour l'a jugé" |
| `applies` | Applies the cited decision's rule to the facts of this case, usually a binding ruling (a preliminary ruling in the referring or a later national case) | "gelet op het arrest ... volgt dat", "toegepast op deze zaak"; "applying the criteria set out in"; "en application de l'arrêt" |
| `distinguishes` | Holds that the cited decision does not govern this case because the facts or the question differ | "anders dan in", "niet vergelijkbaar met", "daarvan onderscheidt deze zaak zich"; "unlike in", "can be distinguished", "differs from"; "à la différence de", "se distingue de" |
| `departs_from` | Declines to follow, criticises, overrules or revisits the cited decision | "in afwijking van", "komt terug van", "houdt niet langer vast aan"; "departs from", "no longer", "overrule", "reconsider"; "s'écarte de", "revient sur" |
| `refers` | Mentions the decision without treating it: background, a list of authorities, the parties' arguments, procedural history | listing ("zie ook", "see also", "voir également"), "appellant beroept zich op", "the applicant relied on" |

Stored labels are these five. Annotators may also mark `unclear`; those
mentions are left out of training and stored with `treatment_label = NULL`.

`follows` vs `applies` is the hardest line. If the pilot's inter-annotator
agreement on that pair stays below kappa 0.6, merge them into `follows` (the
user-facing distinction that matters is rely / distinguish / depart) and record
the merge in the label documentation.

Out of scope here: mentions in the ECtHR "relevant domestic law and practice"
sections (decision D4: `case_citation_mention.section`), which quote other
courts rather than treat them, and the citing party's arguments (labelled
`refers`, not the court's treatment).

## Unit and input

One example = one mention: the citing paragraph with the citation marked.

- Text: the paragraph from `case_segment` when `segment_id` is set; otherwise
  the `case_text` window around `char_start`/`char_end` (sentence-aligned,
  up to ~1,500 characters before and ~500 after), falling back to the stored
  `context` (~300 characters, often too short to see a "however" two
  sentences later).
- The citation is wrapped in marker tokens (`[CIT] ... [/CIT]`) so the model
  knows which of several cited cases in the paragraph is meant.
- Metadata features: citing and cited corpus (RS, CJEU, ECHR), relation type
  (`cites`, `national_follow_up`, ...), citing court level, language. A
  `national_follow_up` mention is a strong prior for `applies`.

Languages: RS Dutch; ECtHR English and French; CJEU in every EU language, of
which only English, French and Dutch are classified at first (the model choice
below would allow more later).

## Building the labelled sample

1. **Population.** Mentions loaded by the Phase 2 extractors on db-dev
   (`rs_text_*`, `echr_text_*`, `cjeu_text_echr`), plus, once mentions exist for
   them, within-court citations. Hand-labelled data is stored in this repository
   (`migration/verify/treatment/`, JSONL keyed by natural keys: citing ECLI,
   cited ECLI or CELEX, `text_field`, `language`, `char_start`, `md5(raw_text)`),
   never in production tables. The texts are public case law.
2. **Stratification.** By corpus pair (RS→CJEU, RS→ECtHR, ECtHR→CJEU,
   CJEU→ECtHR, within-court), by language, and by cue presence. `departs_from`
   and `distinguishes` are rare (to be measured; expect a few percent), so a
   third of the sample is drawn from mentions whose paragraph matches a
   distinguishing/departing cue list (the table above, extended during the
   pilot), and the rest at random, with the cue-sampled part weighted back
   when reporting.
3. **Sizes.** Pilot: 300 mentions, double-annotated, to fix the guidelines and
   measure agreement and class frequencies. Then ~2,000 training mentions
   (single annotation, 20% double-annotated for drift) and a held-out test set
   of 600 (all double-annotated and adjudicated), stratified as above. If a
   class has fewer than 60 test examples, sample more of it by cue before
   training.
4. **Annotators.** Two legally trained annotators (law students or researchers
   reading Dutch, English and French) with written guidelines and examples per
   label and language; disagreements adjudicated by a third. Target: Cohen's
   kappa ≥ 0.7 overall on the pilot before scaling up.
5. **Seed examples already in hand.** The gold set (`migration/verify/
   research-2026-11-24/goldset/data.js` on the roadmap branch) marks four RS
   decisions as "Applies the ruling" for Tjebbes and X, Y and Z (e.g.
   HR:2020:593, RVS:2020:423), and the referral/follow-up pairs; they become
   `applies` examples once their mention rows exist. They are not enough to
   train on, only to check the guidelines.
6. **Model-assisted annotation (optional, owner decision).** A large language
   model can pre-label candidates to speed up annotation, with every label
   reviewed by a person and the pre-label hidden from the second annotator.
   Using an external API sends public court texts to a third party; a local
   open-weight model avoids that. Neither runs in a default code path.

## Candidate models

All take the marked paragraph (≤ 512 tokens) and output a distribution over the
five labels.

| Candidate | Languages | Size | Notes |
|---|---|---|---|
| Cue rules + logistic regression on TF-IDF n-grams | per language | tiny | Baseline and fallback; explains itself; CPU only |
| XLM-RoBERTa base / large | 100 languages | 280M / 560M | Strong multilingual default |
| Legal-XLM-R base / large (multilingual legal pretraining on court texts incl. NL, EN, FR) | EU languages | base / large | Best-matched domain; check licence and tokenizer coverage of Dutch court style in the pilot |
| mDeBERTa-v3 base | 100 languages | 280M | Often better than XLM-R base at the same size |
| Monolingual pair: RobBERT (NL), LEGAL-BERT (EN), CamemBERT / JuriBERT (FR) | one each | 110M–125M | Three models to train and serve; only if multilingual models trail by > 5 points macro-F1 |
| Instruction-tuned LLM, zero/few-shot | many | 7B+ | Comparison point and annotation aid, not the production classifier (cost, latency, reproducibility) |

Recommendation: fine-tune a multilingual legal encoder (Legal-XLM-R base, with
XLM-R base and mDeBERTa-v3 base as the alternatives tried in the pilot), with
citation markers and the metadata features as prefix tokens, class-weighted
loss for the rare labels, 3 seeds, and temperature scaling for calibration on
a validation split. One model for all languages keeps serving simple.

## Compute estimate

Volume (estimate). Phase 2 is expected to add roughly 40,000–55,000 cross-court
links plus ~11,000 opinion links (roadmap); at about 1.5 mentions per link that
is ~60,000–100,000 mentions. If mentions are later backfilled for within-court
citations as well (`echr_edge` alone has 428,581 rows and `rs_formal_relation`
~292,000, before the RS and Cellar citation rows), the order of magnitude
becomes 1–3 million mentions.

| Job | Hardware | Estimate |
|---|---|---|
| Fine-tune a base model, 2,000 examples × 5 epochs × 3 seeds, 512 tokens | 1 × T4/A10/L4 GPU | well under 1 GPU hour |
| Same | 8-core CPU | ~4–8 hours (feasible overnight) |
| Fine-tune a large model, same data | 1 × A10/L4/A100 | ~1–3 GPU hours |
| Inference, base model, 100,000 mentions | 1 × T4-class GPU, fp16 | ~10–20 minutes |
| Same | 8-core CPU (ONNX / int8) | ~2–5 hours |
| Inference, base model, 3 million mentions | 1 × T4-class GPU | ~3–6 GPU hours |
| Same | 8-core CPU | ~2–6 days |
| LLM zero-shot comparison on the 600-mention test set | local 7–8B model on 1 GPU, or an API | < 1 GPU hour; API cost in the single-digit euros at current prices |

Cost: at typical rented-GPU prices (roughly €0.4–1.5 per hour for T4/A10/L4
class), training several candidates plus a full 3-million-mention backfill
stays under ~€50 of GPU time. The labelling (2,900 mentions, ~1–2 minutes
each, plus double annotation) is the real cost: on the order of 100–150 hours
of annotator time.

Does CPU suffice? For the Phase 2 volume and the weekly runs (a few hundred to
a few thousand new mentions a week), yes: a base model on the ETL worker's CPU
classifies a weekly batch in minutes, and training a base model on CPU is an
overnight job. A GPU is worth renting only for trying large models or for a
one-off backfill over millions of within-court mentions. No GPU is needed to
decide whether the approach works.

## Storage

`case_citation_mention` already has the columns (`0009`):

- `treatment_label` text: one of `follows`, `applies`, `distinguishes`,
  `departs_from`, `refers`; NULL when not classified or below threshold.
- `treatment_model` text: model name and version, e.g.
  `treatment-legalxlmr-base/1.0.0` (plus the training data hash in the model
  card).
- `treatment_confidence` real (0–1, already CHECKed): the calibrated
  probability of the stored label.

Keeping it insert-only (ground rule 3):

- **Weekly and backfill runs**: classify in the extraction step, before loading.
  The link artifact's mention objects gain optional `treatment_label`,
  `treatment_model` and `treatment_confidence` fields (an additive change to
  `artifact.py` and `link_loader.py`), so every new mention is inserted with its
  label. The classifier reads only the artifact and the database, never the
  network, and sits behind its own flag (default off), like the extractors.
- **Mentions loaded before the classifier exists** have NULL labels. Filling
  them is an update of NULL columns and needs its own gate and the owner's
  go-ahead (count before/after, a precision sample on the updated rows).
  Simplest is to have the classifier ready before the historical mention
  backfill is loaded, so the gate is never needed for history.
- **A new model version** is also an update of existing rows. If the owner
  wants re-labelling without updates, or wants several model versions side by
  side, add a separate insert-only table instead
  (`case_citation_mention_treatment (mention_id, label, model, confidence,
  created_at)`, unique on `(mention_id, model)`), and treat the `0009` columns
  as the approved model's copy. Decide this before the first production load.

Nothing reads these columns today; serving them (an optional field on edges,
R7.3) and counts per treatment come later, after the evaluation below.

## Evaluation

- **Test set**: the 600 adjudicated mentions, never used for training or model
  selection; the model is chosen on a validation split of the training data.
- **Metrics**: macro-F1 over the five labels, per-class precision and recall,
  confusion matrix, all broken down by corpus pair and language; inter-annotator
  agreement on the same set as the ceiling.
- **Calibration**: expected calibration error after temperature scaling; per
  label, choose the confidence threshold at which test precision is ≥ 0.90 and
  store the label only above it (NULL below). For `departs_from` the threshold
  aims at ≥ 0.95, because a wrong "departed from" misleads more than a missing
  one.
- **Baselines to beat**: cue rules + logistic regression, and the LLM zero-shot
  comparison. If the fine-tuned model does not beat the cue baseline by a clear
  margin on `distinguishes` and `departs_from`, ship the cue baseline (cheaper,
  explainable) or nothing.
- **Acceptance** (proposal for the owner): macro-F1 ≥ 0.75 on the test set,
  precision ≥ 0.90 for every stored label at its threshold, and no language
  more than 10 points below the others; otherwise the labels stay internal.
- **Monitoring**: 50 newly classified mentions a month hand-checked; per-label
  rates per week compared with the training distribution to catch drift (for
  example after a new court or language is added).

## Plan

1. Guidelines and the 300-mention pilot sample from db-dev, double-annotated;
   measure agreement and class frequencies; decide whether to merge
   `follows`/`applies`.
2. Baselines (cue rules, logistic regression) and one base encoder on CPU;
   measure real training and inference times and replace the estimates above.
3. Full annotation (2,000 + 600), model comparison, calibration, thresholds.
4. Artifact and loader fields, classifier step behind its own flag, tests;
   weekly runs on db-dev only.
5. Owner decisions: storage variant (columns vs separate table), acceptance
   thresholds, whether historical mentions are labelled (gated update or
   classify-before-load), whether treatment is shown in the API.

## Open decisions for the owner

- Label set: five labels, or four with `follows` and `applies` merged.
- Whether an external LLM API may be used for annotation assistance (public
  texts, but sent to a third party), or only a local model.
- Storage: `0009` columns only (re-labelling is an update) or an additional
  insert-only treatment table per model version.
- Acceptance thresholds and whether labels below them are stored at all.
- Annotation budget (~100–150 hours).
