# Mercado Libre Flex → SimpliRoute Integration

Automated service that polls Mercado Libre for new Flex orders and registers
them as visits in SimpliRoute, without human intervention.

> **Status:** Day 1 — scaffolding + OAuth (work in progress, more features
> landing each day).

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

## Running tests

```bash
pytest -v
```

Unit tests use in-memory SQLite and `respx` to mock the Mercado Libre API
responses — no network or Docker required.

## Project layout

```
app/
  core/        # config, logging, DB session
  db/          # SQLAlchemy models + Base
  ml/          # Mercado Libre OAuth + API clients
  simpliroute/ # SimpliRoute client (coming Day 3)
  scheduler/   # cron job + two-stage write (coming Day 4)
  alerts/      # Telegram alerting (coming Day 5)
  geocoder/    # address geocoding (coming Day 3)
tests/
  unit/        # fast, isolated tests
  integration/ # end-to-end tests
migrations/    # Alembic migrations
```

## Token rotation (runbook)

Mercado Libre issues a new `refresh_token` on every refresh. The service:

1. Refreshes the access token 5 minutes before expiry.
2. Persists the new `refresh_token` in the same DB transaction.
3. A weekly job (to be added Day 5) forces a preventive refresh if the
   current refresh token is older than 5 months, since the max lifetime is 6.

If you ever see `invalid_grant` on refresh, it means the refresh-token chain
is broken. Re-authorize via `/oauth/authorize`.

## Environment variables

See [`.env.example`](.env.example) for the full list with defaults.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `ModuleNotFoundError: psycopg` | dev deps not installed | `pip install -e '.[dev]'` |
| `connection refused` on port 5432 | Postgres not running | `docker compose up -d postgres` |
| `/oauth/callback` returns 400 | `code` param missing or expired | Restart flow from `/oauth/authorize` |
| `/oauth/callback` returns 502 | ML rejected the code (bad client_id/secret/redirect) | Verify `.env` matches the ML app settings |
