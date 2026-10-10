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

## Knowledge base (policies, procedures, FAQs)

```bash
make seed-knowledge                              # knowledge_base/*.md through the API (make dev) ...
make seed-knowledge API_URL=http://localhost:8080   # ... or through the Docker stack
```

`knowledge_base/` holds eleven synthetic policies, procedures, guidelines and FAQs (one of
them restricted to the Legal department, and two versions of the procurement policy). The
command logs in as `admin@docintel.local`, uploads every file, waits for the worker and exits
non-zero if a file does not end ACTIVE or SUPERSEDED; files already loaded are reported as
`ALREADY_PRESENT`, so it is safe to re-run. Ask questions on the **Knowledge** page; search
business documents in plain language on the **Search** page.

Embeddings: with a `GEMINI_API_KEY`, passages are embedded by Gemini (INTERNAL and PUBLIC
documents only — `AI_EXTERNAL_MAX_SENSITIVITY`); without one, search is full text only and
still works. For local vectors set `EMBEDDING_PROVIDER=fastembed` (after
`cd backend && uv sync --extra local-embeddings`; the model downloads on first use) or
`hashing` (offline, lexical). After changing the provider run `make reembed`. Answers need an
LLM; without one the Knowledge page shows the retrieved passages (`Passages only`).

## AI analysis (investigation agent)

On the **AI analysis** page (or **Investigate** on a document) ask e.g. "Can we pay this
invoice?", "Why does invoice INV-2026-0042 from Kestrel not match its order?" or "Who must
approve payment terms longer than 60 days?". The worker runs the investigation: it finds the
documents, reads their fields and evidence, runs the rules, retrieves the policies that apply
and recommends one action — a review request may be created for you (untick it to prevent
that); payment and duplicate rejection are only proposed. Without an LLM the investigation is
fully deterministic; with `GEMINI_API_KEY` (or Ollama) the model also plans and writes checked
findings, for INTERNAL and PUBLIC documents only. Use synthetic documents only with the free
Gemini tier.

## Workflows, approvals and reports

On an invoice or contract (document page → **Workflows and reports**) start **Invoice
processing** or **Contract review**. The worker checks the document, investigates it (and, for
a contract, compares it with the previous version) and proposes one action: holds and legal
reviews are carried out at once (they go to the review queue); payment, duplicate rejection
and contract approval wait on the **Workflows** page for a manager, a vendor clarification for
a reviewer. Whoever started the workflow, owns the document or uploaded the version cannot
decide it — try it: start a workflow as `analyst@docintel.local`, then approve it as
`manager@docintel.local`. Each finished workflow produces a report (**Reports**; download as
Markdown or JSON, **Verify** re-renders it and compares the SHA-256). Administrators see the
**Audit log** and manage **Users**; managers see their department's audit trail. To start
workflows automatically after processing, set `WORKFLOW_AUTO_START=INVOICE_PROCESSING` (or
`CONTRACT_REVIEW`, comma-separated).

## MCP clients

1. Create a personal token on the **API tokens** page (choose the scopes; it is shown once).
2. stdio (a desktop assistant or IDE on this machine): put the token in `.env` as
   `MCP_API_TOKEN=...` (never commit it) and configure the client to run
   `make mcp` — or `uv run --env-file ../.env docintel mcp` from `backend/`.
3. Streamable HTTP: `make mcp-http`, then point the client at `http://127.0.0.1:8001/mcp`
   with the header `Authorization: Bearer <token>`. To serve beyond localhost set
   `MCP_ALLOWED_HOSTS` and `MCP_PUBLIC_URL` and put TLS in front of it.

The client gets the same tools as the agent (document search, fields, evidence, rules,
comparisons, policy search, review requests, reports, workflow status), limited by the token's scopes and your role;
every call is logged and audited, and revoking the token takes effect on the next call.

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
| `make evaluate` | Run every evaluation suite (OCR, classification, tables, extraction, discrepancies, versions, retrieval, search, agent, workflow; ~45 min) → `evaluation/reports/`. Retrieval, search, agent and workflow create and drop a scratch database on the `TEST_DATABASE_URL` server |
| `make mcp` / `make mcp-http` | MCP server over stdio (needs `MCP_API_TOKEN`) / streamable HTTP on port 8001 |
| `make seed-knowledge` | Load `knowledge_base/` through the API |
| `make reembed` | Add vectors of the configured embedding model to passages that have none (after switching `EMBEDDING_PROVIDER`, or after a provider outage) |
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
| A knowledge document says *full-text only* | No embedding provider is configured, the document is above `AI_EXTERNAL_MAX_SENSITIVITY` for an external embedder, or the provider failed (the note on the document page says which): configure one / use a local provider, then `make reembed` |
| Knowledge answers show *Passages only* | No LLM is configured (`GEMINI_API_KEY`), `RAG_GENERATION_ENABLED=false`, or every source was above the external sensitivity limit |
| *Insufficient evidence* for a question you expect to be answered | The knowledge base does not contain the question's terms (the default lexical evidence check); rephrase with the policy's wording, or check the document is ACTIVE and in force for the date asked |
| `make seed-knowledge`: `Unknown department in front matter: 'Legal'` | The Legal department does not exist yet: run `make seed` first |
| Docker build fails with TLS errors behind a corporate proxy | Your proxy intercepts TLS; build on a network without interception or add your corporate CA to the base images |
