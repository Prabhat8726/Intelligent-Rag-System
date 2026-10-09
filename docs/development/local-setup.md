# Local Development Setup

## Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Docker + Compose v2 | Docker 29, Compose 5 | Runs PostgreSQL + pgvector and the full stack |
| Python | 3.13 | Managed by uv (`uv` downloads it if missing) |
| uv | 0.11.x | https://docs.astral.sh/uv/getting-started/installation/ |
| Node.js | 22 LTS (≥ 22.13) | Frontend tooling |
| GNU make, curl, python3 | any recent | Developer commands and smoke test |
| Tesseract OCR | 5.x with English + OSD data | Needed by the worker and the tests on the host (`apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd`, `brew install tesseract`). The Docker image includes it. Verify with `make check-ocr` |

## First run

```bash
make env          # creates .env with a random JWT secret (never overwrites an existing .env)
# edit .env: set SEED_USER_PASSWORD (min 12 chars) and, optionally, GEMINI_API_KEY
#   (safe placeholder: GEMINI_API_KEY=replace_with_real_key; put the real key only in .env)
#   values containing spaces must be quoted: SEED_USER_PASSWORD="correct horse battery staple"
make setup        # uv sync + npm ci
make seed         # starts Postgres in Docker, applies migrations, creates demo users and vendors
make check-ai     # optional: verifies your Gemini key (or Ollama model) with real calls
```

Without a key everything except the AI fallbacks works: classification uses the local model
and field extraction uses the layout rules; uncertain documents go to review. With
`LLM_PROVIDER=ollama` a self-hosted model takes the LLM role instead of Gemini (see
[configuration](configuration.md#local-model-ollama)).

`make seed` also creates the demo **vendor master** (the five synthetic vendors with tax IDs,
currency and payment terms). Extracted vendor names are resolved against it; managers and
admins can add vendors or aliases through `/api/v1/vendors`.

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
make dev          # API with --reload on :8000, worker, Vite on :5173 (Postgres in Docker)
```

Open http://localhost:5173.  Uploaded files are stored under `backend/storage/` (git-ignored).
`make worker` processes whatever is queued and exits, if you prefer not to keep a worker running. Vite proxies `/api` and `/health` to the API, so the browser
sees a single origin (no CORS configuration needed). API docs: http://localhost:8000/docs.

## Option B — production-like stack in Docker

```bash
make up           # builds images; db -> migrate -> api + worker -> web, waits for health checks
make seed-docker  # demo users inside the stack
make smoke        # end-to-end checks through nginx, including an upload processed by the worker
```

Open http://localhost:8080. `make logs` follows logs, `make down` stops the stack,
`make reset-db` destroys the database and document-storage volumes.

## Synthetic documents

```bash
make generate-documents                          # 37 documents (12 scenarios, seed 42) in synthetic_data/generated/
make process                                     # upload them through the API (make dev) and wait for processing
make process API_URL=http://localhost:8080       # ... or through the Docker stack (make up)
```

Each bundle has a purchase order, delivery note and invoice plus a `.json` ground-truth file
listing the intended discrepancies; `manifest.json` describes the dataset. `make process` logs
in as `analyst@docintel.local` with `SEED_USER_PASSWORD`, writes `ingest-report.json` next to
the dataset and exits non-zero when a document fails, is rejected or does not finish
(`REVIEW_REQUIRED` counts as processed; add `INGEST_FLAGS=--require-completed` to fail on it).
Some synthetic documents are meant to need review: every bundle except the clean ones carries
a discrepancy (price, quantity, short delivery, tax rate, vendor, missing order reference,
wrong printed total, resent invoice), and scanned copies may have fields OCR misreads. They
appear in the **Review queue** (reviewer, analyst, manager and admin roles) with the reason,
and each document page shows its checks, comparison and review history. Uploading the same
dataset again is allowed: the copies are flagged as duplicates and go to review.

## Everyday commands

| Command | What it does |
|---|---|
| `make test` | Backend (unit + integration + security, real Postgres) and frontend tests |
| `make lint` | ruff, ruff format check, mypy --strict, eslint, tsc |
| `make format` | Auto-format backend code |
| `make migrate` | Apply migrations to the local database |
| `make match` | Re-run comparisons, rules and review tasks for every processed document (after upgrading to Phase 5, or after changing rules) |
| `make worker` | Process queued jobs on the host until the queue is empty |
| `make check-ocr` | Verify Tesseract, the configured languages and TSV output |
| `make evaluate` | Run the OCR, classification, table, extraction, discrepancy and version-comparison evaluations (~35 min) → `evaluation/reports/` |
| `make llm-usage` | LLM calls, tokens and estimated cost per day for the last 7 days (from `llm_calls`) |
| `cd backend && uv run --env-file ../.env alembic revision -m "..."` | New migration (write it by hand, then `alembic check`) |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Missing .env` | Run `make env` |
| `warning: Failed to parse environment file` from uv | A value in `.env` contains spaces without quotes: wrap it in double quotes |
| Documents stay `Queued` | No worker is running: `make dev` starts one, or run `make worker`; in Docker check `docker compose ps worker` |
| Worker exits with `OCR engine not found` or `language data missing` | Install Tesseract and the languages in `OCR_LANGUAGES` (see prerequisites); `make check-ocr` |
| Many documents end in `Needs review` | Open one: the banner names the reason. `Document type is uncertain` → correct it (reviewer role), or configure `GEMINI_API_KEY` for the LLM fallback; low OCR confidence → check the scan quality; `Required fields are missing` / `Some extracted values are uncertain` → check or correct the values under *Extracted data*; `Extracted amounts or dates do not add up` → the document (or a misread value) is inconsistent |
| Upgraded from Phase 4 and nothing is in the review queue | Documents processed before Phase 5 have no comparisons or rule results yet: `make match` (the migration already opened tasks for documents that were in review) |
| An invoice says `Referenced purchase order is not on file` | Upload the order (same department): matching re-runs and the warning clears; or the order number on the invoice was misread → correct it under *Extracted data* |
| Vendor column stays empty | The printed vendor name did not match the vendor master: run `make seed` (creates the demo vendors), or add the vendor / alias via `/api/v1/vendors` |
| `ProviderBudgetExceededError` in an extraction's signals | `LLM_DAILY_REQUEST_BUDGET` is used up for today (resets 00:00 UTC); `make llm-usage` shows the count |
| Port 5432 already in use | Another Postgres is running. Set `POSTGRES_PORT=5433` and update `DATABASE_URL`/`TEST_DATABASE_URL` in `.env` |
| `/health/ready` returns 503 with `schema is not at migration head` | Run `make migrate` (or `make up`, which runs the migrate service) |
| `JWT_SECRET_KEY looks like a placeholder` | You are in `staging`/`production` with the example secret: generate a real one |
| `make check-ai`: `model ... is NOT available` | The model was retired or isn't enabled for your key: pick one from the printed list and update `GEMINI_MODEL` / `GEMINI_FAST_MODEL` |
| `make check-ai`: `ProviderRateLimitError` | Free-tier quota exhausted; wait or lower `LLM_REQUESTS_PER_MINUTE` |
| Docker build fails with TLS errors behind a corporate proxy | Your proxy intercepts TLS; build on a network without interception or add your corporate CA to the base images |
