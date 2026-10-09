# 12 — Technology Choices & Free/Low-Cost Strategy

Versions were checked against PyPI / npm / the Gemini SDK source on 2026-10-08.
Exact versions are locked in `backend/uv.lock` and `frontend/package-lock.json`.

## 1. Backend

| Concern | Choice | Why | Alternatives considered |
|---|---|---|---|
| Language | Python 3.13 | Best AI/document ecosystem; typed | — |
| Package manager | **uv** (lockfile, fast, standard `pyproject.toml`) | Reproducible installs in CI/Docker | Poetry (slower), pip-tools |
| Web framework | **FastAPI** 0.14x + Pydantic 2 | Async, OpenAPI out of the box, validation | Django (heavier ORM coupling), Litestar |
| ASGI server | Uvicorn | Standard, production-ready behind a proxy | Hypercorn |
| ORM / migrations | **SQLAlchemy 2.1** (async) + **Alembic** | Mature, typed, explicit migrations | SQLModel (thin layer, less control) |
| DB driver | **psycopg 3** | One driver for async app *and* sync migrations/scripts; LISTEN/NOTIFY for the queue | asyncpg (async only) |
| Database | **PostgreSQL 17 + pgvector 0.8** | Relational + vectors + FTS + queue + audit in one service; iterative HNSW scans for filtered search | Pinecone/Qdrant (extra service, cost), Elasticsearch |
| Job queue | **Postgres `SKIP LOCKED` queue** (own, ~200 LOC, Phase 2) | Transactional enqueue, no Redis, visibility in SQL | Celery+Redis, Dramatiq, procrastinate |
| Auth | **PyJWT** + **pwdlib[argon2]** | Both are what current FastAPI docs recommend; python-jose and passlib are effectively unmaintained | Authlib, external IdP (OIDC later) |
| Settings | pydantic-settings | Typed env config with validation, `SecretStr` | dynaconf |
| Logging | **structlog** (JSON in prod) | Structured, contextvars for request ids, redaction processor | std logging only |
| Metrics | prometheus-client (Phase 11) | De-facto standard, free | OpenTelemetry metrics (can be added) |
| LLM SDK | **google-genai 2.x** | Google's current unified SDK; `google-generativeai` is legacy | REST by hand |
| Local LLM | **Ollama** HTTP API via httpx (`/api/chat` with a JSON-schema `format`, Phase 4) | Self-hosted models keep confidential documents in the deployment (ADR-029); no extra SDK — one small client with the same retries, errors and accounting as Gemini | `ollama` Python package (thin wrapper), vLLM / llama.cpp server (OpenAI-compatible; possible later) |
| Agent framework | **LangGraph 1.x** (Phase 7) | Explicit state graphs, conditional edges, mature | Custom state machine, CrewAI |
| MCP | official `mcp` Python SDK (Phase 7) | Reference implementation | — |
| PDF | **pypdfium2** 5.x (validation, rendering, text layer with character boxes) | Permissive licence, one parser for every PDF task, C speed | PyMuPDF (AGPL), pdfplumber (second parser of untrusted input; dropped in Phase 3, ADR-021), Docling (heavy torch deps; optional future upgrade) |
| OCR | **Tesseract 5** called as a subprocess (TSV output) | Free, local, word boxes + confidences; subprocess gives real timeouts | pytesseract (thin wrapper, adds nothing), PaddleOCR / docTR / EasyOCR (torch-heavy) |
| Image maths | NumPy 2 + SciPy (deskew, line detection) | Vectorized, already required by scikit-learn | OpenCV (large binary for two operations) |
| Fuzzy matching | RapidFuzz 3 | Fast, MIT; CER/WER, evidence quotes, OCR-damaged labels, vendor names | thefuzz, python-Levenshtein |
| Vendor name search | PostgreSQL **pg_trgm** (GIN trigram index, `similarity()`) as a pre-filter, RapidFuzz for the final score (Phase 4) | Scales the vendor master without loading it into memory; extension ships with PostgreSQL | Full-text search (poor on short names), loading all vendors per document |
| Structured extraction | Own layout extractor (labels, letterhead, table headers) + Pydantic schemas for the LLM (Phase 4, ADR-028) | Works without a key or network; every value carries page, quote and box | LLM-only extraction (cost, injection exposure), Docling / LayoutLM (torch, training data) |
| Classifier | scikit-learn 1.9 (TF-IDF + calibrated LR) | Calibrated probabilities, tiny, private, trains in seconds | Fine-tuned transformer (cost, data needs) |
| Local embeddings | fastembed (ONNX) `bge-base-en-v1.5` | No torch, 768-d to match Gemini | sentence-transformers (torch) |
| Synthetic docs | reportlab + Pillow + Faker | Deterministic PDF generation with ground truth | — |
| Tests | pytest, pytest-asyncio, httpx `ASGITransport`, **respx** (HTTP transport mocks) | Real SDK code paths tested without network | — |
| Quality | ruff (lint+format), mypy (strict) | Fast, comprehensive | black+flake8+isort |

## 2. Frontend

| Concern | Choice | Why |
|---|---|---|
| Framework | **React 19** + TypeScript 6.0 | Required by the prompt; TS 6.0 instead of 7.0 because `typescript-eslint` supports `<6.1` |
| Build | **Vite 8** | Fast dev server, simple proxy to the API |
| Styling | **Tailwind CSS 4** (`@tailwindcss/vite`) | Required; v4 needs no PostCSS config |
| Routing | React Router 7 | Stable, well-known API |
| Server state | TanStack Query 5 | Caching, polling for async jobs (analysis, processing) |
| Tests | Vitest 5 + Testing Library + jsdom 29 | jsdom 29 because 30 requires Node ≥ 22.22.2 |
| Lint | ESLint 10 flat config + typescript-eslint + react-hooks | |
| Serving | nginx (alpine) | Static files + reverse proxy + headers |

## 3. Infrastructure

| Concern | Choice | Why |
|---|---|---|
| Containers | Docker, multi-stage builds, non-root | Required; small images |
| Local orchestration | docker compose with health checks and a one-shot `migrate` service | Ordered, reproducible startup |
| CI | GitHub Actions | Required; free for public repos |
| Object storage (prod) | S3-compatible API (AWS S3, Cloudflare R2, MinIO) | One implementation covers most clouds; R2 has a free tier |
| Hosting (Phase 11) | Single VM with docker compose (e.g. an always-free ARM VM) + managed backups | Workers need a long-running process; most PaaS free tiers sleep or disallow workers |

## 4. Free / low-cost strategy

| Cost driver | Strategy |
|---|---|
| LLM | Gemini free tier for development **with synthetic data only** (free-tier terms forbid sensitive data — C1) or a local Ollama model ($0, private); deterministic-first pipeline; local classifier; layout extraction first and the LLM only when it is not confident (`EXTRACTION_LLM_MODE=auto`); one extraction call per document version, cached by input hash; client-side rate limiting; daily request budget (`LLM_DAILY_REQUEST_BUDGET`); every call accounted in `llm_calls` |
| Embeddings | Batched, cached by content hash; local fastembed option = $0 |
| OCR | Tesseract locally = $0 |
| Vector DB / queue / search | All inside PostgreSQL = no extra services |
| Storage | Local FS in dev; R2/MinIO in staging |
| CI | GitHub Actions free minutes; LLM evals not run on every PR |
| Hosting | One small VM runs the whole compose stack |
