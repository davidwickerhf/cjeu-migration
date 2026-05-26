# cjeu-migration

Orchestrates a full-corpus CJEU extraction via [`cellar-extractor`](https://github.com/maastrichtlawtech/cellar-extractor) and publishes the result as a [HuggingFace dataset](https://huggingface.co/docs/datasets) — two Parquet tables (`cases.parquet`, `fulltexts.parquet`) plus a dataset card and the field catalogue.

Designed for unattended long-running execution. Survives interruptions, retries failed windows, and won't re-scrape windows that already completed.

## At a glance

```
                   ┌───────────────────────┐
   .env / CLI ───► │  config.Config        │
                   └─────────┬─────────────┘
                             │
            ┌────────────────┴────────────────┐
            │                                 │
   windowing.iter_windows                manifest.Manifest
    (sd..ed → Window[])             (JSON state, atomic writes)
            │                                 │
            └──┬──────────────┬───────────────┘
               │              │
       runner._scrape_pending  runner._consolidate
               │              │
        scraper.scrape_window  consolidate.consolidate_cases
        (cellar-extractor +      consolidate.consolidate_fulltexts
         tenacity retries)       consolidate.write_dataset_card
               │              │
       per-window CSV+JSON   workspace/dataset/
       in workspace/windows/   {cases,fulltexts}.parquet
                                README.md
                                FIELDS.md
                                       │
                                       ▼
                              huggingface_push.push_dataset
                              (HfApi: create_repo + upload_file)
```

## Setup

```bash
cd cjeu-migration
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
# Fill in HUGGINGFACE_TOKEN and HF_DATASET_REPO.
```

`cellar-extractor` is pinned to `maastrichtlawtech/cellar-extractor@dev`; pip installs straight from git.

## Run

```bash
# Full corpus (default 1954-01-01 → today, month windows).
cjeu-migrate run

# Specific date range (override via .env).
START_DATE=2020-01-01 END_DATE=2020-12-31 cjeu-migrate run

# Skip the HF upload (local-only).
cjeu-migrate run --skip-upload

# Re-run consolidation + push without re-scraping (after fixing missing artifacts manually).
cjeu-migrate run --consolidate-only

# Inspect manifest state without doing anything.
cjeu-migrate status
```

## Configuration

All knobs live in `.env`. See `.env.example` for the full set.

| Variable | Default | Purpose |
|---|---|---|
| `HUGGINGFACE_TOKEN` | _required for upload_ | HF Hub PAT with write scope. |
| `HF_DATASET_REPO` | `maastrichtlawtech/cjeu-cases` | Target dataset slug. |
| `WORKSPACE_DIR` | `./workspace` | Per-window CSV/JSON outputs + manifest. **Mount persistent on Vast.ai.** |
| `START_DATE` / `END_DATE` | `1954-01-01` / today | Date window. Inclusive on both ends. |
| `WINDOW` | `month` | `month` / `quarter` / `year`. Smaller = more retry granularity. |
| `MAX_ECLI_PER_WINDOW` | `10000` | Safety cap on `cellar-extractor.get_cellar_extra`. |
| `EXTRACTOR_THREADS` | `10` | Worker threads inside `cellar-extractor`. |
| `MAX_WINDOW_RETRIES` | `3` | Retries before a window is marked `exhausted` and skipped. |
| `SKIP_UPLOAD` | `0` | Set to `1` to stop after the consolidate step. |
| `NTFY_TOPIC_URL` | _empty (disabled)_ | If set (e.g. `https://ntfy.sh/cjeu-migrate-davidwickerhf-xyz`), pushes a manifest summary every `NTFY_INTERVAL_SECONDS` plus on start / finish / fatal error. Subscribe via the [ntfy app](https://ntfy.sh) on your phone. |
| `NTFY_INTERVAL_SECONDS` | `1800` | Period (in seconds) for the periodic progress push. |
| `NTFY_AUTH_TOKEN` | _empty_ | Bearer token for private / self-hosted ntfy instances. Not needed for `ntfy.sh` public topics. |

## Output layout

```
workspace/
├── manifest.json                       ← per-window state (atomic writes)
├── windows/
│   ├── cases/
│   │   ├── 1954-01.csv
│   │   ├── 1954-02.csv
│   │   └── ...
│   └── fulltexts/
│       ├── 1954-01.json
│       └── ...
└── dataset/                            ← uploaded to HF
    ├── cases.parquet
    ├── fulltexts.parquet
    ├── README.md                       ← HF dataset card
    └── FIELDS.md                       ← field catalogue (from cellar-extractor)
```

## Tests

```bash
# Offline suite — no network, fast.
pytest -q

# Live smoke test — fetches one real month from CELLAR (~30–60s, no HF upload).
RUN_SMOKE=1 pytest tests/test_smoke_integration.py -v
```

## Deployment notes

See [`scripts/deploy_vastai.md`](scripts/deploy_vastai.md) for the operator runbook.

## Failure handling

- **Per-window retries**: each window is retried up to `MAX_WINDOW_RETRIES` with exponential backoff (2s, 4s, 8s, …). Failures don't abort the loop.
- **Restart resumability**: the manifest is read on every start. `completed` windows are skipped, `failed` windows retried up to the cap, `exhausted` windows are left alone for human attention.
- **Partial corpus uploads**: even when some windows exhaust their retries, the consolidate + push step still runs. The dataset card notes the coverage gap.
- **HF upload errors**: surfaced from the underlying `huggingface_hub` call; runner exits with a non-zero status so an orchestrator can restart and rely on idempotency.

## License

Apache-2.0.
