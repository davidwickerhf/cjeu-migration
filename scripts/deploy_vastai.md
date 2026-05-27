# Vast.ai deployment runbook

This is a CPU + network-bound job. Pick the cheapest instance with reliable network egress to EU endpoints (CELLAR is hosted in Luxembourg; pick an EU region if available).

## Required template specs

- Image: any `python:3.11-slim` or `ubuntu:22.04` with Python 3.10+.
- Disk: **150 GB persistent volume** — the v2 corpus (multi-language fanout, every ECLI carries ~10 EU-language bodies) produces ~2 GB of cases CSVs + ~80–100 GB of fulltext JSON + ~6–10 GB of parquet, plus headroom. Mount on `/workspace`.
- RAM: **8 GB** is enough; we never load the full corpus into memory at once.
- CPU: 4-8 vCPU is plenty — the bottleneck is upstream rate limiting (InfoCuria + CELLAR), not local CPU.
- GPU: not needed.

> **v1 vs v2 sizing**: v1 (single-language) needed ~30 GB; v2 carries ~10× more fulltext data because every ECLI now ships in ~10 EU languages and pre-2001 cases are no longer dropped. Always provision for v2 unless explicitly running a single-language test build.

## One-time setup on the instance

```bash
apt-get update && apt-get install -y git python3 python3-venv

git clone https://github.com/davidwickerhf/cjeu-migration.git
cd cjeu-migration

python3 -m venv .venv
source .venv/bin/activate
pip install -e .

cp .env.example .env
# Edit .env: HUGGINGFACE_TOKEN, HF_DATASET_REPO, WORKSPACE_DIR=/workspace
```

`WORKSPACE_DIR=/workspace` is critical — Vast.ai instances are ephemeral; only the mounted volume survives a restart. The manifest and per-window CSVs live there, which is what makes resumability work across instance reboots.

## Run it

```bash
# In a tmux/screen session so a dropped SSH doesn't kill the job.
tmux new -s migrate
source .venv/bin/activate
cjeu-migrate run 2>&1 | tee /workspace/run.log
```

Detach: `Ctrl-b d`. Re-attach: `tmux a -t migrate`.

## Monitoring

```bash
# Manifest status (without scraping).
cjeu-migrate status

# Live log tail.
tail -f /workspace/run.log

# Disk usage.
du -sh /workspace/*
```

## Recovering from interruptions

1. Reconnect to the instance.
2. Re-attach the tmux session or re-launch with the same command.
3. Completed windows are skipped automatically. Failed windows retry up to `MAX_WINDOW_RETRIES`.

## Cleanup before destroying the instance

```bash
# Confirm everything was pushed.
cjeu-migrate status   # all 'completed'
# Pull the dataset back locally for archival if you want.
huggingface-cli repo clone --type dataset $HF_DATASET_REPO ./archive
# Then destroy the instance via the Vast.ai dashboard.
```

## Expected wall time

| Scope | v1 (single-lang) | v2 (multi-lang + pre-2001 fallback) |
|---|---|---|
| One month (e.g. 2020-01) | 30–60 s | 5–10 min |
| One year | 8–10 min | ~1.5 h |
| Full corpus (1954 → today, ~869 windows) | 12–18 h | **14–20 h** |

v2 is slower because every ECLI now triggers ~10 InfoCuria blob fetches (one per language) plus, for pre-2001 cases, an additional CELLAR SPARQL + manifestation fetch. Bottleneck is the per-bucket pacing (50 ms = 20 req/s); local CPU and threads don't help further.

These are with default `EXTRACTOR_THREADS=10`. Endpoint health is the dominant variable; expect ±30% variance.

## Cost notes

At ~$0.10/h for a basic CPU instance:
- v1 full-corpus run: $1.20–$1.80
- v2 full-corpus run: **$1.40–$2.00** (slightly longer, same hourly rate)

The pipeline does no GPU work — explicitly pick a non-GPU instance unless you've reserved one for other reasons. Persistent volume costs add ~$0.10/GB/month — a 150 GB volume parked between runs is ~$15/month, so destroy it when you're done unless you plan to re-run within a few weeks.

## Rate-limiting and 429 handling

The default pacing (`INFOCURIA_MIN_INTERVAL_SECONDS = 0.05` in `cellar_extractor.eurlex_scraping`) gives 20 req/s per bucket and is verified safe for both InfoCuria's CDN and CELLAR's REST endpoint. If you see persistent `429 Too Many Requests` in the logs:

1. The retry-with-backoff (`tenacity`) already retries with exponential delay — short bursts of 429s are handled automatically.
2. For sustained 429s, edit `INFOCURIA_MIN_INTERVAL_SECONDS` to `0.1` (10 req/s) in the installed `cellar-extractor`; restart the run. Resumability ensures already-completed windows are skipped.

## v2 extraction — what to change vs the v1 runbook

When PR #6 in `cellar-extractor` has merged to `dev`, the existing `cjeu-migration` pin (`cellar-extractor@dev`) picks up the v2 code automatically. Until then, pin to the feature branch directly:

```bash
pip install -e .   # NOTE: edit pyproject.toml first if pulling pre-merge code:
#   cellar-extractor @ git+https://github.com/davidwickerhf/cellar-extractor.git@feat/multilang-and-sector6-cellar-fallback
```

Other v2-specific differences from v1:

- **150 GB volume** (was 30 GB) — fulltext JSON inflates ~10×.
- **`fulltexts.parquet` ~6-10 GB on HF** (was 325 MB) — the row-group + page-index layout already handles this for the viewer.
- **Fresh workspace** — do NOT resume from a v1 manifest. The window outputs schema is the same, but the per-window JSON files would mix single-lang (v1) and multi-lang (v2) shapes if you resumed. Delete `/workspace/manifest.json` and `/workspace/windows/` before starting v2.
- **After extraction**, the post-publish cleanup script (`scripts/cleanup_hf_dataset.py`) now knows to dedup on `(ecli, text_language)` for fulltexts, so it's safe to run on the v2 output without collapsing language variants.
