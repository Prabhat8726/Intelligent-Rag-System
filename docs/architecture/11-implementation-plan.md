# 11 — Phased Implementation Plan & Acceptance Criteria

Each phase ends with a status report (COMPLETED / IN PROGRESS / BLOCKED / NEXT
STEP), green CI, and only claims what was executed.

Phase numbering follows the master prompt. **This Phase 0 combines the
prompt's Phase 0 (Discovery & Architecture) and Phase 1 (Foundation)** so that
architecture decisions are validated by running code immediately.

| Phase | Scope | Key deliverables | Exit criteria (summary) |
|---|---|---|---|
| **0 — Architecture & Foundation** | Requirements, architecture, schema, API, agent, RAG, security, evaluation design; runnable foundation | `docs/`, backend skeleton (config, logging, errors, health, DB, migrations, auth+RBAC, audit, AI provider layer + Gemini), frontend shell, Docker, CI, Makefile | See §1 below |
| 2 — Document ingestion | Upload, validation, storage abstraction, versions, job queue, worker, synthetic generator v1 | `POST/GET/DELETE /documents`, `LocalStorage` + S3-compatible storage, PG job queue + worker, `make generate-documents` | Valid files stored + job queued atomically; invalid/oversized/spoofed rejected (security tests); worker processes jobs with retries/leases |
| 3 — OCR & understanding | Per-page inspection, native text, Tesseract OCR, layout blocks, tables, classification, sensitivity gate | `document_pages`, `document_tables`, classifier + LLM fallback, page preview images | Mixed PDFs handled per page; CER/WER measured on synthetic-noisy; classification metrics reported |
| 4 — Structured extraction | Schemas, extraction, repair, evidence, normalization, confidence, LLM usage tracking, Ollama provider | `document_extractions`, `extracted_fields`, `llm_calls`, `/extraction`, `/evidence` | Field metrics measured; malformed JSON handled; every field has provenance or is flagged |
| 5 — Comparison & rules | Comparison engine, rule engine, duplicates, versions diff, review queue | `/comparisons`, `/rules`, `/review-tasks`, contract version diff | Discrepancy P/R/F1 on scenarios; rules configurable; CI regression suite |
| 6 — Knowledge & RAG | KB ingestion, chunking, embeddings, hybrid retrieval, citations, semantic search, fastembed local provider | `/knowledge/*`, `/search` | Retrieval metrics measured with ablations; access filters proven by tests |
| 7 — Agent | LangGraph graph, tool registry, planner, guardrails, MCP server, API tokens | `/analysis`, `mcp` entrypoint | Agent scenario success/tool-selection measured; injection tests pass |
| 8 — Workflow automation | Workflows, HITL state machine, executors, reports, audit API, user management | `/workflows/*`, `/reports`, `/audit-logs`, `/users` | Maker-checker enforced; transitions audited; reports reproducible |
| 9 — Frontend | Dashboard, inbox, viewer with evidence highlights, comparison, AI analysis, approvals, audit, search; refresh tokens | Full SPA | E2E (Playwright) for the demo path |
| 10 — Evaluation | Datasets, all suites, reports, regression gates, demo script | `make evaluate`, `make demo` | README metrics generated from reports |
| 11 — Productionization | Prometheus metrics, rate limiting, hardening, deployment configs (staging/prod), performance tests, runbooks, import-linter | `/metrics`, deploy docs | Deployment claimed only after it is actually performed |

## 1. Phase 0 acceptance criteria

Status as of 2026-10-08. ✅ = verified by an automated test or a recorded command;
⏳ = cannot be verified in the build environment (reason given).

Documentation
- ✅ Requirements analysis with contradictions/resolutions (`01`)
- ✅ Architecture, components, repository structure (`02`), data flow (`03`)
- ✅ ER diagram + table catalogue for all phases (`04`)
- ✅ API specification + role/permission matrix (`05`, matrix locked by `tests/unit/test_permissions.py`)
- ✅ AI/ML architecture incl. confidence model and model choices checked against current Gemini docs/SDK (`06`)
- ✅ RAG design (`07`), agent graph + tools + HITL + MCP (`08`)
- ✅ Security architecture + security test plan (`09`), evaluation plan (`10`)
- ✅ Technology choices with justification and free-tier strategy (`12`), decision log (`13`)
- ✅ Configuration reference and local setup guide (`docs/development/`)

Foundation
- ✅ `uv sync --frozen` installs a locked backend environment; `ruff` and `mypy --strict` pass on `src` and `tests`
- ✅ Settings validate types and refuse weak/placeholder JWT secrets and wildcard CORS outside local/test (`test_config.py`)
- ✅ Structured logging (console/JSON) with request ids and secret redaction (`test_logging.py`)
- ✅ RFC 9457 responses for HTTP, validation and unhandled errors; validation errors never echo input (`test_api_security.py`)
- ✅ Security headers + request-id middleware (`test_health.py`)
- ✅ `GET /health` and `GET /health/ready`; 503 when the database is unreachable (`test_health.py`)
- ✅ Migration creates `vector`, `departments`, `users`, `audit_logs`; upgrade → downgrade → upgrade works; models match migrations (`test_database.py`, `alembic check`)
- ✅ `audit_logs` rejects UPDATE/DELETE/TRUNCATE at DB level (`test_database.py`)
- ✅ Login (argon2id), uniform errors, timing equalization, lockout, `/auth/me`, RBAC dependency (`test_auth_api.py`, `test_api_security.py`)
- ✅ Login success/failure/lockout and authorization denials are audited
- ✅ `LLMProvider`/`EmbeddingProvider` + Gemini implementations tested through the real SDK with HTTP mocked at the transport (`test_gemini_provider.py`, 24 tests)
- ⏳ `make check-ai` against the real Gemini API — **run by the user**: no API key exists in the build environment (the command's no-key path is tested)
- ✅ CLI `seed` (idempotent) and `create-user`; seeding refused in staging/production
- ✅ Frontend: login → protected layout → status page; ESLint, `tsc`, 12 Vitest tests and production build pass; verified in headless Chromium against the Docker stack (no CSP violations)
- ✅ Images build; `docker compose up --wait` reaches healthy db → migrate → api → web; smoke test passes through nginx; containers run as non-root
- ✅ CI workflow passes `actionlint` (incl. shellcheck); audit/secret-scan commands verified locally (pip-audit: no known vulnerabilities; npm audit: 0; gitleaks: clean for committed files)
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)
- ✅ `.env.example` complete; `.env` git-ignored; no secrets committed
