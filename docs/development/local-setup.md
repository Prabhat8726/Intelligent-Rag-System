# Local Development Setup

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Docker + Compose v2 | Docker 29, Compose 5 | Runs PostgreSQL + pgvector and the full stack |
| Python | 3.13 | Managed by uv (`uv` downloads it if missing) |
| uv | 0.11.x | https://docs.astral.sh/uv/getting-started/installation/ |
| Node.js | 22 LTS (≥ 22.13) | Frontend tooling |
| GNU make, curl, python3 | any recent | Developer commands and smoke test |

## First run

```bash
make env          # creates .env with a random JWT secret (never overwrites an existing .env)
# edit .env: set SEED_USER_PASSWORD (min 12 chars) and GEMINI_API_KEY
make setup        # uv sync + npm ci
make seed         # starts Postgres in Docker, applies migrations, creates demo users
make check-ai     # optional: verifies your Gemini key with real API calls
```

Demo users (password = `SEED_USER_PASSWORD`):

| Email | Role | Department |
|---|---|---|
| `admin@docintel.local` | ADMIN | — (organization-wide) |
| `manager@docintel.local` | MANAGER | Finance |
| `analyst@docintel.local` | ANALYST | Finance |
| `reviewer@docintel.local` | REVIEWER | Finance |
| `viewer@docintel.local` | VIEWER | Finance |
| `procurement.analyst@docintel.local` | ANALYST | Procurement |
| `legal.reviewer@docintel.local` | REVIEWER | Legal |

## Option A — hot-reload development (recommended while coding)

```bash
make dev          # API with --reload on :8000 + Vite on :5173 (Postgres in Docker)
```

Open http://localhost:5173. Vite proxies `/api` and `/health` to the API, so the browser
sees a single origin (no CORS configuration needed). API docs: http://localhost:8000/docs.

## Option B — production-like stack in Docker

```bash
make up           # builds images; db -> migrate -> api -> web, waits for health checks
make seed-docker  # demo users inside the stack
make smoke        # end-to-end checks through nginx
```

Open http://localhost:8080. `make logs` follows logs, `make down` stops the stack,
`make reset-db` destroys the database volume.

## Everyday commands

| Command | What it does |
|---|---|
| `make test` | Backend (unit + integration + security, real Postgres) and frontend tests |
| `make lint` | ruff, ruff format check, mypy --strict, eslint, tsc |
| `make format` | Auto-format backend code |
| `make migrate` | Apply migrations to the local database |
| `cd backend && uv run --env-file ../.env alembic revision -m "..."` | New migration (write it by hand, then `alembic check`) |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Missing .env` | Run `make env` |
| Port 5432 already in use | Another Postgres is running. Set `POSTGRES_PORT=5433` and update `DATABASE_URL`/`TEST_DATABASE_URL` in `.env` |
| `/health/ready` returns 503 with `schema is not at migration head` | Run `make migrate` (or `make up`, which runs the migrate service) |
| `JWT_SECRET_KEY looks like a placeholder` | You are in `staging`/`production` with the example secret: generate a real one |
| `make check-ai`: `model ... is NOT available` | The model was retired or isn't enabled for your key: pick one from the printed list and update `GEMINI_MODEL` / `GEMINI_FAST_MODEL` |
| `make check-ai`: `ProviderRateLimitError` | Free-tier quota exhausted; wait or lower `LLM_REQUESTS_PER_MINUTE` |
| Docker build fails with TLS errors behind a corporate proxy | Your proxy intercepts TLS; build on a network without interception or add your corporate CA to the base images |
