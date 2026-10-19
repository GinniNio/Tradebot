# Managed Foundation Runbook

This runbook is for the isolated `tradebot.app` managed research service.

## Required services

- One GitHub repository.
- One Neon Postgres database.
- One single-instance Render web service.

No Redis, Kafka, MongoDB, VPS fleet, self-hosted Solana node, direct pool-swap service, signing worker or trading wallet is part of PR A.

## Render service

Render uses `render.yaml` and builds the Docker image from `Dockerfile`.

The container command starts only:

```bash
uvicorn tradebot.app:app --host 0.0.0.0 --port 10000
```

Do not point Render at `main.py`, `server.py`, `run_all.py` or any legacy flat runtime module.

The container runs `alembic upgrade head` before starting uvicorn, so each
deploy applies its own migrations. If a migration fails, the new instance
exits and Render keeps the previous deploy running.

## Environment

Set these in Render environment configuration:

```text
APP_ENV=production
RESEARCH_EXECUTION_ENABLED=false
LAUNCH_TRACKER_ENABLED=false
DATABASE_URL=<Neon pooled or normal application URL>
SCANNER_LOCK_DATABASE_URL=<Neon direct non-pooled URL>
DEXSCREENER_MAX_CONCURRENCY=2
DEXSCREENER_TIMEOUT_SECONDS=10
DEXSCREENER_MAX_RETRIES=2
DEXSCREENER_RATE_BUDGET_PER_MINUTE=60
HELIUS_MAX_CONCURRENCY=1
HELIUS_TIMEOUT_SECONDS=10
HELIUS_MAX_RETRIES=2
HELIUS_RATE_BUDGET_PER_MINUTE=30
JUPITER_MAX_CONCURRENCY=1
JUPITER_TIMEOUT_SECONDS=10
JUPITER_MAX_RETRIES=2
JUPITER_RATE_BUDGET_PER_MINUTE=30
RUGCHECK_MAX_CONCURRENCY=1
RUGCHECK_TIMEOUT_SECONDS=10
RUGCHECK_MAX_RETRIES=2
RUGCHECK_RATE_BUDGET_PER_MINUTE=20
```

Do not configure `SOLANA_PRIVATE_KEY`. The managed service rejects startup when that variable is present.

## Migrations

Run Alembic only after `DATABASE_URL` points at the intended Neon development or production database:

```bash
alembic upgrade head
```

PR A does not import SQLite data. Legacy cohort export/import belongs to a later PR.

The container applies migrations on start (see Render service above).
`/healthz` shows the applied revision under `database.migration.revision`.

## Research experiment (market_watch_v2)

From `market_watch_v2` the scanner measures outcomes automatically:

- Every token's first qualifying observation (score >= 65) starts +1h,
  +24h, +3d and +7d price checks from that observation's price. Once per
  token. The primary horizon is 24h, fixed in `tradebot/experiment.py`.
- A fixed hash-selected half of tokens that pass all five hard filters but
  score below 65 are tracked the same way as the comparison cohort.
- Each cycle also rechecks up to 300 tokens first seen 1-7 days ago, in
  batches of 30 pairs per Dexscreener call. Rechecks that fail the hard
  filters are counted but not stored.
- A check with no valid price inside its tolerance window (1h: 15 min,
  24h: 2 h, 3d: 6 h, 7d: 12 h) is closed as `unresolved` with a reason. No
  price is ever invented for it.
- Each cycle writes a `scan_cycles` row with counts and rejection reasons.
- Operator Watch/Reject decisions remain optional notes; they do not affect
  the experiment.

Weekly report: open `/report` on the service URL (same dashboard login).
It shows last successful scan, anything needing attention, outcomes,
collection counts and rejection reasons, with summary and observations CSV
downloads. `/` redirects there. The same report from a terminal:

```bash
DATABASE_URL=<neon url> python -m tradebot.verdict           # last 7 days
DATABASE_URL=<neon url> python -m tradebot.verdict --days 14 --json
```

The first review after seven full days is a collection checkpoint
(throughput, rejection reasons, completed and unresolved checks), not a
profitability verdict.

## Health and doctor

Check the service:

```text
GET /healthz
```

Run doctor inside the same environment:

```bash
python -m tradebot.doctor --json
```

The scanner is active only on the instance that holds the Postgres session-level advisory lock. A second overlapping instance must remain API-live with scanner state `standby`.
