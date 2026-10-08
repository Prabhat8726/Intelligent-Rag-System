# 13 — Architecture Decision Log

Short ADRs: context → decision → consequences. New decisions are appended.

### ADR-001 — Modular monolith in one Python package
* **Context**: API, worker, agent and MCP share the domain model and need atomic writes (document + job + audit).
* **Decision**: one package `docintel` with sub-packages and three entrypoints; Alembic migrations live inside the package (`docintel/db/migrations`, see ADR-011).
* **Consequences**: + no model duplication, one lockfile, simple deploys. − discipline needed to keep boundaries (layering rules; import-linter in Phase 11). Deviates from the suggested top-level `workers/ ai/ mcp/ migrations/` folders.

### ADR-002 — PostgreSQL as job queue (no Redis)
* **Context**: prompt says Redis only if genuinely needed; we need durable background jobs.
* **Decision**: `processing_jobs` table, claim with `FOR UPDATE SKIP LOCKED`, leases + heartbeats, exponential backoff, `LISTEN/NOTIFY` for low-latency wakeups.
* **Consequences**: + enqueue is transactional with domain writes, queue state is queryable for the dashboard, one fewer service. − throughput ceiling far above our needs (thousands of jobs/min); revisit only with measured need.

### ADR-003 — pgvector with fixed 768-d embeddings
* **Context**: providers must be swappable; vector columns have fixed dimensions.
* **Decision**: 768-d for all providers (Gemini MRL truncation + normalization; local bge-base). Store `embedding_model` per row; retrieve only same-model vectors; `make reindex` on model change.
* **Consequences**: + swap without schema change. − 768-d is slightly below Gemini's 3072-d quality ceiling (Google reports small MTEB differences); measurable in Phase 10.

### ADR-004 — Agent proposes, humans approve, executors execute
* **Context**: LangGraph supports in-graph interrupts; approvals may take days and graph code changes between deploys.
* **Decision**: graph ends at `AWAITING_APPROVAL` after persisting a `workflow_action`; approval triggers a deterministic allowlisted executor.
* **Consequences**: + approvals are first-class auditable records; no checkpoint version skew; the LLM can never execute high-impact actions. − post-approval steps are not "inside" the graph (acceptable: they are deterministic).

### ADR-005 — Confidence from measured signals, not LLM self-report
* **Context**: prompt forbids treating LLM self-confidence as ground truth.
* **Decision**: weighted signals (OCR, evidence match, type validity, consistency, agreement), weakest-link aggregation, thresholds calibrated in Phase 10.
* **Consequences**: + explainable, testable routing. − needs labelled data to calibrate (synthetic first).

### ADR-006 — No `roles` or `system_metrics` tables
* **Context**: suggested in the prompt's table list.
* **Decision**: role is a constrained column with a code-defined permission map; metrics via Prometheus + source tables.
* **Consequences**: + no privilege escalation through data edits; no duplicated metric storage. − adding a role needs a code change + migration (intended).

### ADR-007 — Sensitivity-gated external AI
* **Context**: Gemini unpaid-tier terms prohibit sensitive/confidential/personal data.
* **Decision**: per-document sensitivity + `AI_EXTERNAL_MAX_SENSITIVITY` routing to local providers or manual review; synthetic data for free-tier development.
* **Consequences**: + compliant by construction. − confidential documents get lower automation until a paid tier or local LLM is configured.

### ADR-008 — Pinned model versions as configuration
* **Context**: Gemini model IDs changed several times in 2025–2026.
* **Decision**: explicit stable model IDs in env (`GEMINI_MODEL`, `GEMINI_FAST_MODEL`, `GEMINI_EMBEDDING_MODEL`); no `-latest` aliases by default; model recorded per call.
* **Consequences**: + reproducible evaluations, one-line upgrades. − someone must update defaults when Google retires models (`make check-ai` surfaces this).

### ADR-009 — Permissive-licence document stack
* **Decision**: pypdfium2 + pdfplumber + Tesseract instead of PyMuPDF (AGPL).
* **Consequences**: + safe for commercial use. − slightly more glue code for rendering vs text.

### ADR-010 — Bearer JWT now, refresh-token cookie in Phase 9
* **Context**: the SPA needs sessions; API/MCP clients need bearer auth.
* **Decision**: Phase 0 issues short-lived access tokens; the SPA keeps the token in memory + `sessionStorage` (tab-scoped). Phase 9 adds httpOnly refresh cookies with rotation and server-side revocation, and moves the access token to memory only.
* **Consequences**: + simple, works for all clients now. − until Phase 9, an XSS could read the tab's token; mitigated by strict CSP and React's escaping, and explicitly tracked.

### ADR-011 — Migrations ship inside the Python package
* **Context**: the readiness probe must compare the database revision with the migration head *of the running build*, and the container image installs the package non-editable.
* **Decision**: Alembic environment and versions live in `docintel/db/migrations` (`script_location = docintel.db:migrations`).
* **Consequences**: + the head revision is always available at runtime (`/health/ready` reports schema drift); one artefact contains code and schema. − deviates from a top-level `migrations/` folder (prompt §41).

### ADR-012 — Pure-ASGI middleware and problem-details everywhere
* **Context**: `BaseHTTPMiddleware` interferes with contextvars and streaming; unhandled exceptions in Starlette bypass app middleware.
* **Decision**: request-context and security-header middleware are pure ASGI; the request-context middleware converts unhandled exceptions into an RFC 9457 500 response, so even crashes carry `X-Request-ID` and security headers and never leak internals.
* **Consequences**: + consistent error contract (tested). − slightly more low-level code.
