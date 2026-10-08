# Configuration Reference

All configuration is read from environment variables by `docintel.core.config.Settings`
(pydantic-settings). Locally they come from the root `.env` file; in containers from the
environment. Variables are case-insensitive. Secrets are typed `SecretStr` and never logged.

Create your `.env` with `make env` — it copies `.env.example` and generates a random
`JWT_SECRET_KEY`. `.env` is git-ignored; never commit it.

## Gemini API key (credential handling)

| Item | Value |
|---|---|
| Where to get it | Google AI Studio → **Get API key** → https://aistudio.google.com/apikey (create or pick a Google Cloud project) |
| Variable | `GEMINI_API_KEY` |
| Location | root `.env` file (local) or your deployment's secret store (staging/production) |
| Safe example | `GEMINI_API_KEY=replace_with_real_key` (placeholder only — never paste real keys into chat, issues, commits or screenshots) |
| Verify | `make check-ai` — lists models available to the key, runs one structured-output call per model tier and one embedding call, and prints latency/token usage. Exit code 0 = working |
| Rotate | Create a new key in AI Studio, update `.env`/secret store, delete the old key |

**Free-tier warning.** Under the Gemini API *unpaid* terms, Google may use prompts and
responses to improve its products and human reviewers may read them; the terms say not to
submit sensitive, confidential or personal information. Use **synthetic documents only** on
the free tier (see [C1](../architecture/01-requirements.md)). Real business documents need a
paid tier or a local model (Phase 4).

Free-tier quotas vary per model and change over time; check **AI Studio → Usage/Rate limits**
for your project and set `LLM_REQUESTS_PER_MINUTE` / `EMBEDDING_REQUESTS_PER_MINUTE` at or
below them.

## Reference

### Application

| Variable | Default | Description |
|---|---|---|
| `APP_ENV` | `local` | `local`, `test`, `staging`, `production`. Staging/production refuse placeholder JWT secrets and wildcard CORS, enable HSTS, default to JSON logs; production disables API docs by default. The Docker image defaults to `production`. |
| `APP_NAME` | Enterprise Document Intelligence Platform | Shown in OpenAPI |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `LOG_FORMAT` | derived | `console` (local default) or `json` (default elsewhere) |
| `API_DOCS_ENABLED` | derived | Swagger UI at `/docs`, ReDoc at `/redoc`, schema at `/openapi.json`. Default: on except in production |
| `CORS_ALLOWED_ORIGINS` | empty | Comma-separated origins. Not needed for the bundled setups (same-origin via Vite proxy / nginx) |

### Database

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | **required** | `postgresql+psycopg://user:pass@host:port/db` (psycopg 3 driver enforced). Containers receive a value built from `POSTGRES_*` |
| `DB_POOL_SIZE` | `10` | SQLAlchemy pool size per process |
| `DB_MAX_OVERFLOW` | `10` | Extra connections under burst |
| `DB_POOL_TIMEOUT_SECONDS` | `10` | Wait for a pooled connection |
| `DB_STATEMENT_TIMEOUT_MS` | `30000` | Server-side `statement_timeout` for every connection |
| `DB_ECHO` | `false` | Log SQL (development only) |
| `TEST_DATABASE_URL` | `postgresql+psycopg://docintel:docintel@localhost:5432/docintel` | Server used by tests; each test session creates and drops its own database |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` / `POSTGRES_PORT` | see `.env.example` | docker compose database bootstrap; keep `DATABASE_URL` consistent |

### Authentication

| Variable | Default | Description |
|---|---|---|
| `JWT_SECRET_KEY` | **required** | ≥ 32 characters; random (`python3 -c "import secrets; print(secrets.token_urlsafe(48))"`) |
| `JWT_ALGORITHM` | `HS256` | Only HS256 is accepted (explicit allow-list) |
| `JWT_ACCESS_TOKEN_TTL_MINUTES` | `30` | Access-token lifetime (1–1440) |
| `JWT_ISSUER` / `JWT_AUDIENCE` | `docintel` / `docintel-api` | Validated on every request |
| `AUTH_MAX_FAILED_LOGINS` | `5` | Failed attempts before lockout |
| `AUTH_LOCKOUT_MINUTES` | `15` | Lockout duration |
| `SEED_USER_PASSWORD` | unset | Password for demo users created by `make seed` (min. 12 chars). Seeding is refused in staging/production |

### Uploads and storage

| Variable | Default | Description |
|---|---|---|
| `UPLOAD_MAX_BYTES` | `26214400` (25 MiB) | Maximum file size. The request limit for `POST /api/v1/documents` is this plus 1 MiB multipart overhead. If you raise it, raise `client_max_body_size` (26m) in `frontend/nginx/default.conf.template` to match |
| `API_MAX_BODY_BYTES` | `1048576` (1 MiB) | Request-body limit for every other endpoint |
| `UPLOAD_MAX_PAGES` | `200` | Maximum pages (PDF) or frames (TIFF) |
| `UPLOAD_MAX_IMAGE_PIXELS` | `50000000` | Maximum width × height for images (decompression-bomb guard) |
| `STORAGE_BACKEND` | `local` | `local` (filesystem) or `s3` (AWS S3 or any S3-compatible service: Cloudflare R2, MinIO, …) |
| `STORAGE_LOCAL_ROOT` | `storage` | Root directory for `local`. Relative paths resolve against the working directory (`backend/` for `make dev`). The Docker image and compose use `/data/storage` on the shared `docstore` volume |
| `S3_BUCKET` | unset | Required when `STORAGE_BACKEND=s3` (validated at startup) |
| `S3_ENDPOINT_URL` | unset | Unset = AWS; set for R2/MinIO |
| `S3_REGION` | unset | Region name (R2 uses `auto`) |
| `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` | unset | Static credentials. Unset = boto3's default chain (instance/task role, `AWS_*` variables, shared config) |
| `S3_KEY_PREFIX` | empty | Prefix for every object key, e.g. `docintel/prod` |

Object keys are `documents/{document_id}/v{n}/original.{ext}`; user-supplied filenames are
never part of a key (ADR-014).

### Background worker

| Variable | Default | Description |
|---|---|---|
| `WORKER_CONCURRENCY` | `1` | Jobs processed concurrently per worker process (1–32). PDF work is serialized per process (ADR-018); add worker processes to scale it |
| `WORKER_POLL_INTERVAL_SECONDS` | `5` | Fallback polling interval; new jobs normally wake the worker immediately via `LISTEN/NOTIFY` |
| `JOB_LEASE_SECONDS` | `300` | A claimed job belongs to its worker for this long; the worker renews the lease every third of it. Expired leases (crashed worker) are reclaimed by any worker |
| `JOB_MAX_ATTEMPTS` | `3` | Attempts per job, including reclaims after a crash, before it is marked `FAILED` |
| `JOB_RETRY_BASE_SECONDS` | `30` | Retry backoff: base × 2^(attempt−1) plus up to 10 % jitter, capped at 1 hour |
| `WORKER_HEARTBEAT_FILE` | unset | File the worker touches while alive; `docintel worker-health` fails if it is older than max(60 s, 3 × poll interval, lease / 2). Compose sets `/tmp/docintel-worker.heartbeat` |

### AI providers

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | LLM implementation (local providers arrive in Phase 4) |
| `EMBEDDING_PROVIDER` | `gemini` | Embedding implementation (local fastembed in Phase 6) |
| `GEMINI_API_KEY` | unset | See above. Blank = not configured |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Default tier: extraction, analysis, RAG answers |
| `GEMINI_FAST_MODEL` | `gemini-3.5-flash-lite` | Fast tier: classification fallback, planning |
| `GEMINI_EMBEDDING_MODEL` | `gemini-embedding-001` | Requested at 768 dimensions and L2-normalized |
| `GEMINI_THINKING_LEVEL` | unset | `MINIMAL`/`LOW`/`MEDIUM`/`HIGH`; unset = model default (not sent) |
| `LLM_TEMPERATURE` | unset | Unset = model default (not sent), as recommended for Gemini 3.x |
| `LLM_TIMEOUT_SECONDS` | `60` | Per-request timeout |
| `LLM_MAX_RETRIES` | `3` | Retries for 408/429/5xx with exponential backoff + jitter |
| `LLM_REQUESTS_PER_MINUTE` | `10` | Client-side token bucket for generation calls |
| `EMBEDDING_REQUESTS_PER_MINUTE` | `60` | Client-side token bucket for embedding calls |
| `EMBEDDING_BATCH_SIZE` | `100` | Texts per embedding request (max 100) |

Model IDs are pinned versions on purpose (reproducible evaluations, ADR-008). If Google
retires a default, `make check-ai` reports it as unavailable — change the variable.

### Containers

| Variable | Default | Description |
|---|---|---|
| `WEB_PORT` | `8080` | Host port for nginx (bound to 127.0.0.1) |
| `API_UPSTREAM` | `http://api:8000` | nginx → API upstream (web image) |
| `FORWARDED_ALLOW_IPS` | `*` in compose | Proxies whose `X-Forwarded-For` uvicorn trusts. Safe in compose because only nginx can reach the API; restrict in other topologies |
