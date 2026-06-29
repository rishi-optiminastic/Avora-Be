# Deploying the Avora backend to a Hetzner VPS (Docker)

This stack runs the API, a self-hosted Postgres, the four background workers, and
a Caddy reverse proxy (automatic HTTPS) on a single VPS.

```
caddy (:80/:443, TLS) ──> api (uvicorn :8000)
                          ├── db        postgres:16  (+ pgdata volume)
                          ├── migrate   one-shot: alembic upgrade head
                          ├── scheduler-eod
                          ├── scheduler-payroll
                          ├── scheduler-autocheckout
                          └── worker-ocr   (separate Tesseract image)
```

Files in this folder:
- `docker-compose.prod.yml` — the stack.
- `.env.prod.example` — copy to `.env.prod` and fill in (real secrets, gitignored).
- `Caddyfile` — reverse proxy + TLS for `$DOMAIN`.
- `backup.sh` — nightly `pg_dump` (cron).

---

## 1. Provision the VPS

- Create a Hetzner Cloud server (Ubuntu 24.04; CX22/CPX21 or larger).
- Point a DNS **A record** for your API host (e.g. `api.example.com`) at the
  server's public IP. TLS won't issue until this resolves.
- Open the firewall to **22, 80, 443** only. Do **not** expose 5432 or 8000 —
  the compose network keeps them internal.

## 2. Install Docker + Compose v2

```bash
curl -fsSL https://get.docker.com | sh        # installs Docker Engine + compose plugin
docker compose version                        # verify v2 is present
```

## 3. Get the code onto the VPS

```bash
sudo mkdir -p /opt/avora && sudo chown "$USER" /opt/avora
git clone <your-repo-url> /opt/avora            # the `be/` backend must be present
cd /opt/avora/be/deploy
```
(Or `rsync` the `be/` directory up if the repo isn't reachable from the VPS.)

## 4. Configure secrets

```bash
cp .env.prod.example .env.prod
# Generate strong secrets:
for v in JWT_SECRET AGENT_TOKEN_PEPPER HR_WEBHOOK_SECRET BIOMETRIC_WEBHOOK_SECRET \
         INVITE_TOKEN_PEPPER POSTGRES_PASSWORD; do echo "$v=$(openssl rand -hex 32)"; done
nano .env.prod                                  # paste secrets + fill every CHANGE_ME
```

Must-get-right values (these are what break a deploy):
- `DOMAIN` / `ACME_EMAIL` — the API hostname + Let's Encrypt email.
- `POSTGRES_PASSWORD` **and** the password inside `DATABASE_URL` — keep them identical.
- `BETTER_AUTH_JWKS_URL` / `_ISSUER` / `_AUDIENCE` — the **deployed dashboard** URL
  (not localhost), or human login fails.
- `CORS_ORIGINS` / `APP_BASE_URL` — the dashboard's https origin.
- `SENDGRID_API_KEY`, `AWS_*`, `OPENROUTER_API_KEY` — for email / S3 / EOD.

## 5. Build and start

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod build
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

Startup order is enforced: `db` becomes healthy → `migrate` runs
`alembic upgrade head` and exits 0 → `api` + schedulers start. Caddy then
fetches a TLS cert for `$DOMAIN` (give it ~30s on first boot).

## 6. Verify

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod ps      # all Up; migrate Exited(0)
docker compose -f docker-compose.prod.yml --env-file .env.prod logs -f migrate   # "migrations_at_head" path
curl https://api.example.com/api/v1/healthz                            # {"status":"ok"} over TLS
curl https://api.example.com/api/v1/readyz                             # DB connectivity check
```
In production the OpenAPI docs are intentionally disabled (no `/docs`).

## 7. Backups (do this on day one)

```bash
./backup.sh                                     # one manual run -> ./backups/*.dump.gz
crontab -e
# 15 2 * * * cd /opt/avora/be/deploy && ./backup.sh >> backups/backup.log 2>&1
```
Restore instructions are in the header of `backup.sh`.

---

## Operating it

```bash
# alias to save typing
alias dc='docker compose -f docker-compose.prod.yml --env-file .env.prod'

dc ps                 # status
dc logs -f api        # tail one service (JSON logs; secrets are redacted)
dc restart api        # restart a service after an env change
```

**Deploying an update:**
```bash
cd /opt/avora && git pull
cd be/deploy
dc build && dc up -d   # migrate re-runs (no-op if already at head), then rolls services
```

## Notes & gotchas

- **One uvicorn process on purpose.** The rate limiter is in-process, so running
  multiple API workers would split its buckets. Scale vertically for now; move to
  Redis-backed limiting before running more than one API replica.
- **Migrations are gated, not automatic.** They run only in the `migrate` one-shot
  (`ENVIRONMENT=production` disables in-app auto-migrate), so there's no
  multi-process migration race.
- **The local `.venv` and `.env` never enter the image** — `be/.dockerignore`
  excludes them; the image builds its own venv with `uv`.
- **OCR worker DB role.** Optionally give `worker-ocr` a least-privilege DB user
  (SELECT screenshots, UPDATE ocr_text/ocr_status) instead of the app role; see
  `be/worker/README.md`.
- **Email deliverability.** Set SPF/DKIM for `EMAIL_FROM`'s domain in SendGrid or
  invites/payroll mail will land in spam.
