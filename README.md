# PMS Backend (FastAPI)

The **only trusted component** of the PMS (Productivity Management System). Every
request from the Next.js dashboard, the Go laptop agent, and the HR webhook is a
*claim to verify* — all authentication, authorization, scoping, and validation
live here. See [CLAUDE.md](CLAUDE.md) for the full security contract.

## Stack

Python 3.12+ · FastAPI · Pydantic v2 · SQLAlchemy 2.0 (async) + asyncpg ·
Alembic · Postgres · uv · pytest · ruff · mypy (strict).

## Layout

```
app/
  main.py             # app factory, middleware, router registration
  core/               # config, security, deps, logging, exceptions, middleware
  api/v1/routes/      # one module per resource
  models/             # SQLAlchemy ORM models
  schemas/            # Pydantic request/response shapes (never ORM models)
  services/           # business logic + authorization (no FastAPI objects)
  repositories/       # the only place DB queries are built (all scoped)
  db/                 # async engine + session factory
migrations/           # Alembic
tests/                # pytest (incl. an authz test per protected endpoint)
```

Strict layering: **route → service → repository → db**.

## Quick start

```bash
uv sync                                  # install deps
cp .env.example .env                     # then fill in secrets (never commit .env)
docker compose up -d db                  # local Postgres
uv run alembic revision --autogenerate -m "initial schema"
uv run alembic upgrade head              # apply migrations
uv run uvicorn app.main:app --reload     # http://localhost:8000  (docs at /docs)
```

## Quality gates (run before any change is "done")

```bash
uv run ruff check . && uv run ruff format .
uv run mypy app
uv run pytest
# or simply:
make check
```

## Security highlights (enforced, with tests)

- **Human auth:** Google OIDC → short-lived JWT in an httpOnly/Secure cookie.
  Signature, expiry, issuer, audience verified on every request; `alg: none`
  rejected by construction. Role/scope are **re-derived from the DB**, never the
  token.
- **Agent ingest** (`POST /api/v1/activity/ingest`): per-device bearer token
  (peppered-hash at rest) + HMAC over the raw body + per-device rate limit +
  **monotonic sequence replay protection** + server-stamped receive time.
- **HR webhook** (`POST /api/v1/hr/sync`): HMAC-verified + optional IP allowlist.
  The payload schema **structurally cannot carry privilege** — HR can never set
  a role. Offboarding soft-deletes (`is_active = False`).
- **Scoped reads:** out-of-scope access returns `404` (not `403`) so existence
  is not leaked. Every protected endpoint has an authorization test.
- **Audit log:** append-only, hash-chained; no mutate/delete endpoint exists.

## Endpoints (v1)

| Method | Path                              | Auth            |
| ------ | --------------------------------- | --------------- |
| GET    | `/api/v1/healthz` / `/readyz`     | none            |
| GET    | `/api/v1/employees`               | session cookie  |
| GET    | `/api/v1/employees/{id}`          | session cookie  |
| PUT    | `/api/v1/employees/{id}/role`     | admin           |
| POST   | `/api/v1/activity/ingest`         | device token + HMAC |
| POST   | `/api/v1/hr/sync`                 | HR HMAC         |
```
