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
