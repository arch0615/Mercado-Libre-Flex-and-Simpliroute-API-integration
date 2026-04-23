# Mercado Libre Flex → SimpliRoute Integration

Automated service that polls Mercado Libre for new Flex orders and registers
them as visits in SimpliRoute, without human intervention. Idempotent,
tolerant to restarts, alerted via Telegram on failures.

## Requirements

- Python 3.11+
- Docker (for local PostgreSQL) or an existing Postgres 14+

## Quick start (local)

```bash
# 1. Clone and enter the repo
git clone <repo-url>
cd mercado-simpliroute

# 2. Create virtualenv + install deps
python3 -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'

# 3. Copy env template and fill in secrets
cp .env.example .env
# edit .env with your real ML / SimpliRoute / Telegram credentials

# 4. Start PostgreSQL
docker compose up -d postgres

# 5. Apply migrations
alembic upgrade head

# 6. Run the API
uvicorn app.main:app --reload --port 8000
```

Once running, open [http://localhost:8000/health](http://localhost:8000/health) — you should see `{"status": "ok", ...}`.

## First-time OAuth authorization (Mercado Libre)

1. Make sure `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, and `ML_REDIRECT_URI` are set
   in `.env`, and that the redirect URI matches the one registered in your ML
   application.
2. Visit [http://localhost:8000/oauth/authorize](http://localhost:8000/oauth/authorize).
3. Log in with the seller account and accept the permissions.
4. You will be redirected to `/oauth/callback` and the token pair will be
   persisted to the `oauth_tokens` table.

## Triggering the sync manually

The scheduler is an HTTP endpoint protected by a shared secret. Railway's
cron hits it every 20 minutes; you can also hit it by hand for testing:

```bash
curl -X POST http://localhost:8000/internal/run \
  -H "X-Internal-Secret: $INTERNAL_SECRET"
```

Response:

```json
{"cron_run_id": 42, "status": "success",
 "processed": 3, "skipped": 0, "manual_review": 0, "errors": 0}
```

## Running tests

```bash
pytest -v
```

Unit tests use in-memory SQLite and `respx` to mock the Mercado Libre and
SimpliRoute APIs — no network or Docker required.

## Project layout

```
app/
  core/        # config, logging, DB session
  db/          # SQLAlchemy models + Base
  ml/          # Mercado Libre OAuth + Orders client
  simpliroute/ # SimpliRoute visit client
  scheduler/   # cron job, two-stage write, advisory lock, watchdog
  alerts/      # Telegram alerting with DB-backed dedup
  geocoder/    # address geocoding (Google / Nominatim) + cache
tests/
  unit/        # fast, isolated tests
  integration/ # end-to-end tests
migrations/    # Alembic migrations
```

## Deploy on Railway

1. Create a new Railway project, add a PostgreSQL addon.
2. Link the GitHub repo.
3. Set all env vars from [.env.example](.env.example). Key ones:
   - `DATABASE_URL` (auto-set by the Postgres addon)
   - `ML_CLIENT_ID`, `ML_CLIENT_SECRET`, `ML_REDIRECT_URI`, `ML_SELLER_ID`
   - `SIMPLIROUTE_TOKEN`
   - `GOOGLE_MAPS_API_KEY` (or `GEOCODER_BACKEND=nominatim`)
   - `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
   - `INTERNAL_SECRET` (long random string)
4. Configure three cron jobs in Railway:

   | Cron | Schedule | Command |
   |---|---|---|
   | Main sync | `*/20 * * * *` | `curl -fsS -X POST $RAILWAY_PUBLIC_DOMAIN/internal/run -H "X-Internal-Secret: $INTERNAL_SECRET"` |
   | Watchdog | `*/10 * * * *` | `curl -fsS -X POST $RAILWAY_PUBLIC_DOMAIN/internal/watchdog -H "X-Internal-Secret: $INTERNAL_SECRET"` |
   | Token health | `0 9 * * 1` | `curl -fsS -X POST $RAILWAY_PUBLIC_DOMAIN/internal/token-health -H "X-Internal-Secret: $INTERNAL_SECRET"` |

   Start command and migrations are handled by [railway.json](railway.json).

5. Re-run the first-time OAuth authorization against the deployed URL
   (`https://<your-project>.up.railway.app/oauth/authorize`) so the token
   pair lives in production.

## Idempotency guarantees

Five layers prevent duplicate visits:

1. **`UNIQUE(ml_order_id)`** on `processed_orders` (DB-level).
2. **Two-stage write:** claim a `pending` row + COMMIT before calling
   SimpliRoute; mark `completed` only after success.
3. **Retry recovery:** on a `pending`/`failed` retry, `find_visit_by_reference`
   queries SimpliRoute for an existing visit with this order's id as
   `reference` and rebinds it instead of creating a duplicate.
4. **`reference = ml_order_id`** on every SimpliRoute visit — both for
   audit and to enable (3).
5. **Advisory lock:** `pg_try_advisory_lock` at the top of `run_sync`
   rejects a second concurrent run cleanly (returns `skipped`).

## Token rotation (runbook)

Mercado Libre issues a new `refresh_token` on every refresh. The service:

1. Refreshes the access token 5 minutes before expiry.
2. Persists the new `refresh_token` in the same DB transaction.
3. A weekly cron hits `/internal/token-health`; if the refresh token is
   older than 5 months, it forces a preventive exchange — ML refresh
   tokens die at 6 months, so this leaves ~1 month of safety.

If you see `invalid_grant` on refresh, the refresh-token chain is broken.
Re-authorize via `/oauth/authorize` from the deployed URL.

## Alerts (Telegram)

All alerts are sent to the configured `TELEGRAM_CHAT_ID`. Every alert has
a stable `dedup_key` so sustained incidents don't spam the channel; the
default dedup window is 15 minutes (see `ALERT_DEDUP_WINDOW_MINUTES`).

| Dedup key | Level | Trigger |
|---|---|---|
| `ml_refresh_failed` | critical | `get_valid_access_token()` couldn't refresh |
| `simpliroute_transient` | critical | all SimpliRoute retries exhausted |
| `simpliroute_permanent` | error | SimpliRoute returned 4xx |
| `manual_review:<reason>` | warning | an order was routed to `manual_review` |
| `cron_watchdog` | critical | no successful cron_run in the watchdog window |
| `refresh_token_aging` | warning | refresh token older than 5 months |
| `cron_run_failed` | critical | `run_sync` raised an unhandled exception |

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `ModuleNotFoundError: psycopg` | dev deps not installed | `pip install -e '.[dev]'` |
| `connection refused` on port 5432 | Postgres not running | `docker compose up -d postgres` |
| `/oauth/callback` returns 400 | `code` param missing or expired | Restart flow from `/oauth/authorize` |
| `/oauth/callback` returns 502 | ML rejected the code (bad client_id/secret/redirect) | Verify `.env` matches the ML app settings |
| `/internal/run` returns 401 | Missing or wrong `X-Internal-Secret` | Check the header value and `INTERNAL_SECRET` env var |
| Visit not appearing in SimpliRoute | Geocoding below threshold | Check `manual_review` table; raise or lower `GEOCODER_MIN_CONFIDENCE` |
| Duplicate visits in SimpliRoute | Extremely unlikely | Check `SELECT COUNT(*) FROM processed_orders WHERE ml_order_id='X'` — UNIQUE should be 1 |
| Telegram silent | `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` missing | Verify env vars, hit the bot manually with `curl https://api.telegram.org/bot$TOKEN/getMe` |

### Quick DB queries

```sql
-- Last 20 processed
SELECT ml_order_id, status, processed_at, simpliroute_visit_id
FROM processed_orders ORDER BY processed_at DESC LIMIT 20;

-- Manual review backlog
SELECT ml_order_id, reason, created_at FROM manual_review
WHERE resolved_at IS NULL ORDER BY created_at;

-- Cron health (last 24h)
SELECT DATE_TRUNC('hour', started_at) AS hour,
       COUNT(*) AS runs, SUM(processed_count) AS processed,
       SUM(errors_count) AS errors
FROM cron_runs WHERE started_at > NOW() - INTERVAL '24 hours'
GROUP BY 1 ORDER BY 1 DESC;
```
