# Vast.ai deployment runbook

This is a CPU + network-bound job. Pick the cheapest instance with reliable network egress to EU endpoints (CELLAR is hosted in Luxembourg; pick an EU region if available).

## Required template specs

- Image: any `python:3.11-slim` or `ubuntu:22.04` with Python 3.10+.
- Disk: **30 GB persistent** — the full corpus produces ~15 GB of CSVs + ~10 GB of fulltext JSON + ~5 GB of parquet. Mount on `/workspace`.
- RAM: **8 GB** is enough; we never load the full corpus into memory at once.
- CPU: 4 vCPU is plenty.
- GPU: not needed.

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

| Scope | Approx. wall time |
|---|---|
| One month (e.g. 2020-01) | 30–60 s |
| One year | 8–10 min |
| Full corpus (1954 → today, ~852 windows) | 12–18 hours |

These are with default `EXTRACTOR_THREADS=10`. Endpoint health is the dominant variable; expect ±30% variance.

## Cost notes

At ~$0.10/h for a basic CPU instance, a full-corpus run costs $1.20–$1.80. The pipeline does no GPU work — explicitly pick a non-GPU instance unless you've reserved one for other reasons.
