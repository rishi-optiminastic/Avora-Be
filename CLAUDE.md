# CLAUDE.md — PMS Backend (FastAPI)

Instructions for any AI assistant or developer working in this repository.
Read the **Golden rules** and **Security rules** before writing or changing code.
When a request conflicts with a security rule, do **not** comply silently — flag it and propose a secure alternative.

---

## 1. Project context

This repo is the **backend** of the PMS (Productivity Management System) — an internal employee-activity and task-tracking tool.

The full system has three parts:

- **Next.js dashboard** — what managers, HR, and employees see. Untrusted client.
- **FastAPI backend (this repo)** — the only trusted component. All enforcement lives here.
- **Go laptop agent** — captures active window, idle time, login/logout and POSTs it. **Assume the agent and its machine are hostile** (users have local admin and will try to evade or forge data).

Employee identity is fed in from a separate **HR system** via webhook (onboard/offboard). HR is the source of truth for the org tree (who reports to whom).

**This backend is the only thing that touches the database.** The agent and the dashboard never reach Postgres directly — they go through this API.

---

## 2. Golden rules (read first)

1. **Never trust the client.** Every value from the agent, the browser, or the webhook is a *claim to verify*, never a fact. Validate and re-derive on the server.
2. **Authorize every endpoint** from the authenticated identity. Never trust a client-supplied role, team, or employee id.
3. **Scope every data query to the caller.** A manager must never be able to read another team's rows, even by guessing an id.
4. **No secrets in code, logs, or responses.** Secrets come from environment/settings only.
5. **Never return ORM models directly.** Always go through a Pydantic response schema so you can't leak a column by accident.
6. **Prefer the explicit, secure option.** If unsure, ask before weakening a control. Match the existing HR project's conventions where they differ from this file.

---

## 3. Tech stack

- **Python 3.12+**, **FastAPI**, **Pydantic v2** (and `pydantic-settings` for config)
- **SQLAlchemy 2.0 (async)** + **Alembic** migrations, **asyncpg** driver, **Postgres**
- **uv** for dependency + env management
- **pytest** + **pytest-asyncio** for tests
- **ruff** (lint + format), **mypy** (strict) for static checks
- Auth: Google OIDC for humans (JWT sessions), per-device tokens + HMAC for agents

Do not add a new dependency without a clear reason. Keep the surface small.

---

## 4. Project structure

```
app/
  main.py            # FastAPI app factory, router registration, middleware
  core/
    config.py        # Settings (pydantic-settings, env-driven)
    security.py      # JWT, password/token hashing, HMAC verify
    deps.py          # shared FastAPI dependencies (get_current_user, scoping)
    logging.py       # structured logging setup + redaction
  api/
    v1/
      routes/        # one module per resource (employees, activity, tasks, ...)
      router.py      # aggregates v1 routers
  models/            # SQLAlchemy ORM models
  schemas/           # Pydantic request/response schemas (NEVER reuse ORM models as schemas)
  services/          # business logic — routes call services, services call repos
  repositories/      # DB access only; all queries scoped here
  db/
    session.py       # async engine + session factory
migrations/          # Alembic
tests/
```

**Layering (strict):** `route → service → repository → db`.
- Routes: parse input, call a service, return a response schema. No business logic, no raw DB access.
- Services: business rules. No FastAPI objects (`Request`, `Depends`) inside services.
- Repositories: the only place that builds queries.

---

## 5. Security rules (NON-NEGOTIABLE)

### 5.1 Trust model
- The agent is **hostile by assumption**. Stamp a **server-side receive timestamp** on ingest; never order or trust by the agent's clock.
- Treat agent-reported activity as a claim. Validate shape, reject impossible values, and flag anomalies (see 5.4) — do not store raw claims as ground truth without checks.
- All enforcement is server-side. There is **no** authorization logic in the Next.js client; assume the bundle is fully readable by users.

### 5.2 Authentication
- Humans: Google OIDC → short-lived signed JWT, delivered in an **httpOnly, Secure, SameSite** cookie. Never localStorage.
- Verify JWT signature, expiry, issuer, and audience on every request. **Reject `alg: none`.** Never accept an unsigned or `HS`-downgraded token.
- Agents: per-device token (or mTLS cert) issued at enrollment, **one credential per device** — never a shared key baked into the binary. Tokens must be revocable and rotatable.

### 5.3 Authorization (apply to EVERY endpoint)
- Derive `role` and `team`/scope from the **server's record** of the authenticated user, looked up by verified identity — **never** from a request field or a client-set claim.
- Resolve the caller's visible employee set on the server and filter by it. PMS scope rules:
  - `executive` → own data only
  - `manager` → own + direct reports
  - `senior_manager` → own department
  - `hr` → attendance / leave / payroll across the org
  - `admin` → everything
  - `it_admin` → device + system health only (no productivity content)
  - `viewer` → read-only within an explicitly granted scope
- **Forbid IDOR.** `GET /employees/{id}/activity` must return 403 (not data) when `{id}` is outside the caller's scope. Object access is checked against the caller, not the URL.
- Enforce scoping in **one place** (a repository helper / dependency), so no endpoint can forget the `WHERE` clause. New read endpoints must use it.

### 5.4 Agent ingestion (`/api/v1/ingest`)
- Authenticate the device (`Depends(verify_device)`); reject unknown/revoked devices.
- Verify the **HMAC signature** of the payload with the device's key. Reject on mismatch.
- Enforce a **monotonic per-device sequence number**; reject reused or out-of-order sequences (replay protection). Dedup on `(device_id, sequence)`.
- Validate the payload with a strict Pydantic model. **Rate-limit per device.**
- Flag (don't trust) evasion signals: agent timestamp vs server time skew, missing heartbeats, suspiciously regular input, activity gaps. Record flags; never silently drop.

### 5.5 HR webhook (`/api/v1/hr/sync`)
- **HMAC-verify** every call against the HR↔PMS shared secret. Reject anything unsigned. IP-allowlist if available.
- The webhook may create/deactivate an employee and set org fields only. It must **never** set `role`, `admin`, or any privilege. Privilege changes happen only inside the PMS by an admin.
- Only sync the minimum fields (id, name, work email, department, reporting manager, status, start date). **Never** pull HR documents or sensitive files into this system.

### 5.6 Data handling, secrets, errors
- **Secrets** come from `Settings` (env) only. Never hardcode; never commit `.env`; never log a secret, token, or raw activity payload.
- **Parameterized queries only** via the ORM. Never build SQL with string formatting/f-strings.
- Validate all input with Pydantic. Never pass unvalidated client data into queries, file paths, or shell.
- Encrypt sensitive columns at rest; TLS in transit everywhere.
- **Errors:** raise `HTTPException` with correct status codes. A global handler returns generic messages — never leak stack traces, SQL, or internal details to the client.
- Use a **least-privilege DB role** (no DDL/superuser from the app).

### 5.7 Audit logging
- Maintain an **append-only** (ideally hash-chained) audit log of sensitive reads/actions: who viewed whose data, exports, admin actions, role changes, device enrollment.
- The audit log is write-only from the app's perspective — never expose an endpoint that mutates or deletes audit rows.

---

## 6. Coding conventions

- **Async everywhere** — `async def` routes, async SQLAlchemy sessions. No sync DB calls in the request path.
- **Type hints are mandatory.** Code must pass `mypy --strict`.
- **Pydantic v2** for all request and response models. Separate `Create`, `Update`, and `Read` schemas; never expose an ORM model as the API shape.
- **Dependency injection** via `Depends` for auth, scope, and sessions — not globals.
- Format and lint with **ruff**; keep functions small and single-purpose.
- **Logging:** structured logs, redact tokens/PII, never log full activity bodies or secrets. No `print`.
- Time is **UTC** internally; convert at the edge only.

---

## 7. API conventions

- Version routes under `/api/v1/...`. Resource-oriented paths, plural nouns.
- Always declare `response_model=`; return the schema, not the ORM object.
- Correct status codes (201 on create, 204 on delete, 403 vs 404 used deliberately — prefer 404 over 403 when even revealing existence leaks scope).
- Paginate all list endpoints; cap page size server-side.
- Idempotent where it should be (ingest, webhook).

---

## 8. Database conventions

- All schema changes via **Alembic migrations**, reviewed. Never `Base.metadata.create_all()` in any non-test environment.
- **Soft-delete** employees on offboard (`is_active = False`) — never hard-delete; retention and audit depend on history.
- Every table: `id`, `created_at`, `updated_at`. Index the hot query paths (device_id, employee_id, date).
- Raw activity is the source of truth; rollups are derived and re-computable.

---

## 9. Testing

- **Every protected endpoint must have an authorization test** proving an out-of-scope user gets 403/404. This is not optional — an endpoint without an authz test is incomplete.
- Test the ingest path for replay rejection, bad HMAC, and bad sequence. Test the webhook for unsigned rejection and that it cannot set roles.
- Use `pytest-asyncio`, a disposable test database, and factories. No network or real external calls in unit tests.

---

## 10. Never do this

- ❌ Trust a role/team/id sent by the client.
- ❌ Return an ORM model directly from an endpoint.
- ❌ Add an endpoint without scope/authorization checks.
- ❌ Build SQL by string interpolation.
- ❌ Hardcode a secret, or log a secret/token/PII/activity body.
- ❌ Store agent-claimed data as truth without server-side validation.
- ❌ Let the HR webhook set privileges, or pull HR documents into this system.
- ❌ `create_all()` against a real database, or hard-delete employees.

---

## 11. Commands

```bash
uv sync                         # install deps
uv run uvicorn app.main:app --reload   # run dev server
uv run pytest                   # run tests
uv run ruff check . && uv run ruff format .   # lint + format
uv run mypy app                 # type check
uv run alembic revision --autogenerate -m "msg"   # new migration
uv run alembic upgrade head     # apply migrations
```

Run `ruff`, `mypy`, and `pytest` before considering any change done.