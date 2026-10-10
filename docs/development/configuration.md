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
paid tier or a local model (`LLM_PROVIDER=ollama`, below).

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

### OCR and document understanding

| Variable | Default | Description |
|---|---|---|
| `TESSERACT_CMD` | `tesseract` | Tesseract 5 executable (must be on `PATH` for the worker; the Docker image includes it). Check with `make check-ocr` |
| `OCR_LANGUAGES` | `eng` | Tesseract language codes joined by `+`, e.g. `eng+deu`; the language data must be installed. More languages = slower OCR |
| `OCR_DPI` | `300` | Resolution at which PDF pages without a text layer are rendered for OCR |
| `OCR_UPSCALE_BELOW_DPI` | `250` | Images below this DPI are upscaled (max 2×) before OCR; `0` disables. Measured to help (ADR-023) |
| `OCR_PAGE_TIMEOUT_SECONDS` | `120` | The OCR process for one page is killed after this; the job is retried |
| `OCR_CONCURRENCY` | `2` | Pages OCR'd in parallel per document (each uses one CPU core) |
| `OCR_REMOVE_RULING_LINES` | `false` | Remove table grid lines before OCR. Measured to make synthetic scans worse; kept for experiments |
| `OCR_REVIEW_BELOW_CONFIDENCE` | `50` | A page whose mean OCR confidence is below this sends the document to review (`LOW_OCR_CONFIDENCE`) |
| `PAGE_PREVIEW_WIDTH` | `1000` | Width in pixels of the page preview images stored per page |
| `CLASSIFICATION_MIN_CONFIDENCE` | `0.7` | Below this the LLM fallback is tried (if allowed); still below → `REVIEW_REQUIRED` |
| `CLASSIFICATION_LLM_FALLBACK` | `true` | Use the LLM for uncertain classifications when a key is configured and the gate allows |
| `CLASSIFICATION_CORPUS_PER_CLASS` | `200` | Synthetic training documents per type for the local model (trained at worker start) |
| `AI_EXTERNAL_MAX_SENSITIVITY` | `INTERNAL` | Highest effective sensitivity whose text may be sent to an external AI provider (`PUBLIC`, `INTERNAL`, `CONFIDENTIAL`, `RESTRICTED`). Effective = max(upload label, detected card numbers/SSNs, type minimum for resumes and bank statements). On the Gemini free tier, upload synthetic data only, whatever this says |

### AI providers

| Variable | Default | Description |
|---|---|---|
| `LLM_PROVIDER` | `gemini` | `gemini` or `ollama` (a self-hosted model server; see *Local model* below) |
| `EMBEDDING_PROVIDER` | `gemini` | Vectors for knowledge and document passages (ADR-042): `gemini` (external, behind the sensitivity gate; needs the key), `fastembed` (local ONNX model, optional dependency `uv sync --extra local-embeddings`, downloads the model on first use), `hashing` (offline and deterministic, **lexical not semantic** — for evaluation, tests and air-gapped demos). Without a usable provider, passages are found by full-text search only |
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
| `FASTEMBED_MODEL` | `BAAI/bge-base-en-v1.5` | Local model for `EMBEDDING_PROVIDER=fastembed`; it must produce 768-d vectors (queries get the model's retrieval instruction prefix) |
| `FASTEMBED_CACHE_DIR` | unset | Where the model is stored (unset = fastembed's default cache) |
| `FASTEMBED_THREADS` | unset | ONNX runtime threads (unset = runtime default) |
| `LLM_DAILY_REQUEST_BUDGET` | `0` | Maximum LLM requests per provider per UTC day across all workers (counted in `llm_calls`); `0` = unlimited. When used up, calls fail fast and documents keep the layout result (ADR-031) |
| `LLM_PRICING` | `{}` | JSON map of model ID → USD per million tokens, e.g. `{"gemini-3.5-flash": {"input_per_mtok": 0.5, "output_per_mtok": 3.0}}` (illustrative numbers — take current prices from the provider's pricing page). Used only for the estimated cost in `llm_calls` and `docintel llm-usage`; models without a price show no cost. No prices are shipped |

Model IDs are pinned versions on purpose (reproducible evaluations, ADR-008). If Google
retires a default, `make check-ai` reports it as unavailable — change the variable.

`make llm-usage` (`docintel llm-usage --days N`) prints calls, failures, tokens and estimated
cost per day, provider, model and purpose from `llm_calls`, and the configured daily budget.

### Local model (Ollama)

With `LLM_PROVIDER=ollama`, classification fallback and field extraction use a model served by
[Ollama](https://ollama.com) inside your deployment. Content does not leave it, so the
external-AI gate does not block confidential documents (ADR-029) — keep `OLLAMA_BASE_URL` on a
host you control. Embeddings still use `EMBEDDING_PROVIDER`. The provider is covered by tests
against a mocked Ollama API; it has not been run against a live Ollama server in the build
environment. `make check-ai` verifies your setup (lists models, one structured call per tier).

| Variable | Default | Description |
|---|---|---|
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama server. From the compose containers use the host's address (e.g. `http://host.docker.internal:11434` where available) |
| `OLLAMA_MODEL` | unset | Model for extraction (required with `LLM_PROVIDER=ollama`), e.g. one you pulled with `ollama pull`. It must support structured output (`format` with a JSON schema) |
| `OLLAMA_FAST_MODEL` | `OLLAMA_MODEL` | Model for classification fallback |
| `OLLAMA_VISION` | `false` | `true` if the model accepts images; then low-confidence OCR pages are attached to extraction requests |
| `OLLAMA_KEEP_ALIVE` | unset | How long Ollama keeps the model loaded (Ollama duration, e.g. `10m`); unset = server default |

### Structured extraction

| Variable | Default | Description |
|---|---|---|
| `EXTRACTION_LLM_MODE` | `auto` | `auto`: call the LLM only when the layout extractor's result would not be auto-accepted; `always`; `never` (layout rules only, no AI calls). The sensitivity gate applies in every mode |
| `EXTRACTION_CONFIDENCE_HIGH` | `0.85` | Document confidence at or above this with no failed consistency check → auto-accepted |
| `EXTRACTION_CONFIDENCE_MEDIUM` | `0.6` | At or above → analyst review; below → mandatory review. Must not exceed `HIGH` |
| `EXTRACTION_ARITHMETIC_TOLERANCE` | `0.01` | Allowed difference in amount checks (qty × price, sums, tax, totals) |
| `EXTRACTION_MAX_PROMPT_CHARS` | `60000` | Page text sent to the LLM is cut at this length (a marker says so) |
| `EXTRACTION_MAX_OUTPUT_TOKENS` | `8192` | Output limit for the extraction call |
| `EXTRACTION_VISION_BELOW_OCR_CONFIDENCE` | `70` | OCR pages below this mean confidence are attached as images when the model accepts images |
| `EXTRACTION_MAX_IMAGES` | `2` | Maximum page images per extraction request |
| `EVIDENCE_FUZZY_THRESHOLD` | `85` | RapidFuzz alignment score (0–100) at which a quote counts as a close match (`FUZZY`) |
| `VENDOR_MATCH_MIN_SCORE` | `85` | Name similarity (0–100) needed to link a printed vendor name to the vendor master (tax IDs and aliases match exactly) |

### Matching and review

Price, quantity and tax-rate tolerances are rule parameters (`/rules`, `rules:manage`), not
settings: one place decides both the comparison and the rule outcome.

| Variable | Default | Description |
|---|---|---|
| `COMPARISON_MIN_CONFIDENCE` | `0.85` | A difference involving a machine-read value below this confidence (and not corrected by a reviewer) is `UNCERTAIN` and its rule warns ("could not be verified") instead of failing. The same threshold decides whether a failed arithmetic check on the document is confirmed |
| `REVIEW_SLA_HOURS` | `{"URGENT": 4, "HIGH": 24, "NORMAL": 72, "LOW": 168}` | Hours from task creation to its due date, per priority (JSON object; priorities left out keep their default; 1–8760) |

### Knowledge base and RAG

Changing the embedding provider or model leaves existing vectors from the old model unused
(queries only match vectors of the configured model): run `make reembed` afterwards.

| Variable | Default | Description |
|---|---|---|
| `KNOWLEDGE_TEXT_MAX_BYTES` | `2097152` | Size limit for Markdown/text knowledge files (PDFs and images use `UPLOAD_MAX_BYTES`) |
| `KNOWLEDGE_CHUNK_TARGET_TOKENS` | `500` | Passages are filled up to about this size within a section (tokens ≈ characters / 4) |
| `KNOWLEDGE_CHUNK_MAX_TOKENS` | `800` | Hard limit: longer paragraphs are split at sentences, longer tables by row groups with the header repeated |
| `KNOWLEDGE_CHUNK_OVERLAP_TOKENS` | `75` | Whole sentences repeated between consecutive passages of one section (never across sections) |
| `RAG_CANDIDATES` | `20` | Candidates taken from each retriever (vector, full text) before fusion |
| `RAG_TOP_K` | `6` | Passages fused with Reciprocal Rank Fusion and given to the answer model |
| `RAG_RRF_K` | `60` | RRF constant |
| `RAG_MIN_TERM_COVERAGE` | `0.25` | Evidence gate: the best of the top five passages must contain this rarity-weighted share of the question's terms … |
| `RAG_MIN_DENSE_SIMILARITY` | `0.5` | … or be at least this similar in embedding space; otherwise "insufficient evidence" and no model call. **Model-specific**: chosen with the hashing model on `kb-queries`; calibrate it for Gemini or fastembed with `make evaluate` before relying on it. Also the minimum similarity for a vector-only match in document search |
| `RAG_MAX_CONTEXT_TOKENS` | `3000` | Budget for the sources sent to the answer model (the top source is always included) |
| `RAG_GENERATION_ENABLED` | `true` | `false` = no generated answers: `/knowledge/query` returns the passages only (`RETRIEVAL_ONLY`) |
| `RAG_MAX_OUTPUT_TOKENS` | `1024` | Output limit for the answer call |

### Investigation agent

| Variable | Default | Meaning |
|---|---|---|
| `AGENT_LLM_ENABLED` | `true` | Plan and analyse with the configured LLM when one is configured and the sensitivity gate allows it; `false` = deterministic planning and analysis (tools, rules, confidence and recommendation work the same) |
| `AGENT_MAX_TOOL_CALLS` | `30` | Tool calls per investigation; past it tools return nothing and the result says which steps were skipped |
| `AGENT_MAX_LLM_CALLS` | `4` | Model calls per investigation (plan + up to two analyses) |
| `AGENT_MAX_DOCUMENTS` | `3` | Documents investigated as subjects (related orders, deliveries and duplicates are added up to twice this) |
| `AGENT_TIMEOUT_SECONDS` | `180` | Wall-clock limit of a run; a run over it fails with that reason |
| `AGENT_TOOL_TIMEOUT_SECONDS` | `30` | Limit per tool call |
| `AGENT_TOOL_MAX_OUTPUT_BYTES` | `65536` | Largest tool result accepted |
| `AGENT_MAX_OUTPUT_TOKENS` | `1536` | Output limit for the analysis call |
| `AGENT_MAX_ACTIVE_RUNS_PER_USER` | `3` | Queued or running investigations per user; more get 429 |

### MCP and API tokens

| Variable | Default | Meaning |
|---|---|---|
| `API_TOKEN_MAX_DAYS` | `90` | Longest lifetime of a personal API token |
| `MCP_API_TOKEN` | — | **Secret.** The token the stdio MCP server (`docintel mcp`) acts with — create it on the API tokens page; never commit it |
| `MCP_PUBLIC_URL` | `http://<host>:<port>` | URL MCP clients use for the streamable HTTP server (protected-resource metadata) |
| `MCP_ALLOWED_HOSTS` | `127.0.0.1:*,localhost:*` | `Host` headers the HTTP server accepts (DNS-rebinding protection); add your hostname when serving beyond localhost |

### Containers

| Variable | Default | Description |
|---|---|---|
| `WEB_PORT` | `8080` | Host port for nginx (bound to 127.0.0.1) |
| `API_UPSTREAM` | `http://api:8000` | nginx → API upstream (web image) |
| `FORWARDED_ALLOW_IPS` | `*` in compose | Proxies whose `X-Forwarded-For` uvicorn trusts. Safe in compose because only nginx can reach the API; restrict in other topologies |
