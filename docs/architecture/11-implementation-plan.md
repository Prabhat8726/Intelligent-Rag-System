# 11 — Phased Implementation Plan & Acceptance Criteria

Each phase ends with a status report (COMPLETED / IN PROGRESS / BLOCKED / NEXT
STEP), green CI, and only claims what was executed.

Phase numbering follows the master prompt. **This Phase 0 combines the
prompt's Phase 0 (Discovery & Architecture) and Phase 1 (Foundation)** so that
architecture decisions are validated by running code immediately.

| Phase | Scope | Key deliverables | Exit criteria (summary) |
|---|---|---|---|
| **0 — Architecture & Foundation** | Requirements, architecture, schema, API, agent, RAG, security, evaluation design; runnable foundation | `docs/`, backend skeleton (config, logging, errors, health, DB, migrations, auth+RBAC, audit, AI provider layer + Gemini), frontend shell, Docker, CI, Makefile | See §1 below |
| **2 — Document ingestion** ✅ | Upload, validation, storage abstraction, versions, job queue, worker, synthetic generator v1 | `POST/GET/DELETE /documents`, `LocalStorage` + S3-compatible storage, PG job queue + worker, `make generate-documents` | Valid files stored + job queued atomically; invalid/oversized/spoofed rejected (security tests); worker processes jobs with retries/leases |
| **3 — OCR & understanding** ✅ | Per-page inspection, native text, Tesseract OCR, layout blocks, tables, classification, sensitivity gate | `document_pages`, `document_tables`, classifier + LLM fallback, page preview images | Mixed PDFs handled per page; CER/WER measured on synthetic-noisy; classification metrics reported |
| **4 — Structured extraction** ✅ | Schemas, extraction, repair, evidence, normalization, confidence, LLM usage tracking, Ollama provider | `document_extractions`, `extracted_fields`, `llm_calls`, `/extraction`, `/evidence` | Field metrics measured; malformed JSON handled; every field has provenance or is flagged |
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

## 2. Phase 2 acceptance criteria

Status as of 2026-10-08, same legend as §1. Test files are under `backend/tests/`.

Upload and validation
- ✅ PDF, PNG, JPEG and TIFF accepted; type taken from magic bytes and required to match the extension and declared MIME type (`unit/test_upload_validation.py`)
- ✅ Through the API, spoofed (HTML, PNG or executable named `.pdf`/declared as PDF, PDF declared as HTML), corrupt, empty and password-protected files are rejected with 415/422 and **nothing is stored** (`security/test_document_security.py::test_malicious_or_invalid_files_are_rejected_and_nothing_is_stored`); too many pages, too many pixels and decompression-bomb headers are rejected by the validator (`unit/test_upload_validation.py::test_unprocessable_content_is_rejected`)
- ✅ Oversized bodies rejected with 413 by declared length and while streaming, before multipart parsing (`unit/test_body_limit_middleware.py`, `security/...::test_declared_oversized_body_is_rejected_before_parsing`, `::test_streamed_oversized_body_without_length_is_cut_off`)
- ✅ Filenames sanitized (NFKC, path, control and bidi characters); they never influence storage keys (`::test_path_traversal_filename_cannot_influence_storage_location`, `unit/test_storage.py::test_document_keys_never_contain_user_text`)
- ✅ Document, version, job and audit row created in one transaction; the stored file is deleted if the transaction fails (`integration/test_documents_api.py::test_blob_is_removed_when_the_database_write_fails`)
- ✅ Exact duplicates accepted and flagged, without revealing documents from other departments (`::test_exact_duplicate_is_accepted_and_flagged`, `security/...::test_duplicate_detection_does_not_leak_other_departments`)

Access and API
- ✅ List (filters, pagination), detail, download, soft delete, reprocess (`integration/test_documents_api.py`, 10 tests)
- ✅ Department scoping: other departments get 404 on read, download, delete and reprocess; same department and admins can read; viewers cannot upload and the denial is audited (`security/test_document_security.py`, 13 tests)
- ✅ Downloads are attachments with `nosniff` and `CSP: sandbox` (`::test_download_streams_original_with_safe_headers`)

Storage
- ✅ Local backend (path confinement, symlink escape blocked, atomic writes) and S3 backend share one contract test suite (`unit/test_storage.py`; S3 runs against moto's S3 server over HTTP)
- ⏳ S3 against a real provider (AWS S3 / R2 / MinIO): not available in the build environment; the backend is configuration-only (`STORAGE_BACKEND=s3`)

Queue and worker
- ✅ Claims are exclusive under concurrency (`SKIP LOCKED`); NOTIFY is delivered only after commit and wakes an idle worker faster than its poll interval; expired leases are reclaimed and the stale worker can no longer write; jobs reclaimed too often fail without running; transient errors retry with exponential backoff, permanent errors fail at once; deleted documents' jobs are cancelled (`integration/test_worker.py`)
- ✅ Worker re-verifies SHA-256 (tampered or missing file → permanent failure) and records per-page inspection: native text vs. needs OCR (`unit/test_inspection.py`, `integration/test_worker.py`)
- ✅ Docker worker runs as a non-root user with a heartbeat health check and a graceful stop period

Synthetic data
- ✅ Deterministic generator (same seed → identical bytes) for 12 scenarios with ground truth; recorded defects are verified to be present in the documents; every generated file passes upload validation (`unit/test_synthetic.py`)
- ✅ End-to-end: generated dataset → API → queue → worker → `COMPLETED` for every document, in-process (`integration/test_ingestion_e2e.py`) and against the Docker stack (`make process API_URL=http://localhost:8080`: 37/37 completed, 2026-10-08)

Frontend and delivery
- ✅ Documents inbox (upload, filters, paging), detail (file facts, job timings, per-page inspection), download, reprocess and delete, permission-aware (`frontend/src/documents/documents.test.tsx`); checked in headless Chromium against the Docker stack with no console errors
- ✅ 292 backend tests, ruff, ruff format, mypy --strict; 20 frontend tests, ESLint, `tsc`, production build
- ✅ `docker compose up --wait` brings db → migrate → api + worker → web to healthy; smoke test passes including an upload processed by the worker
- ✅ CI updated (worker in the container job, dataset generate + ingest) and passes `actionlint`; pip-audit: no known vulnerabilities; npm audit: 0; gitleaks: clean
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)

Not in Phase 2 (by design): OCR, classification, extraction and near-duplicate detection (same invoice number, different file) — Phases 3–5.

## 3. Phase 3 acceptance criteria

Status as of 2026-10-08, same legend as §1. Metrics live in `evaluation/reports/` (ADR-027).

Text extraction
- ✅ Per page: PDF text layer (pypdfium2 words with boxes in displayed orientation, all four page rotations) or OCR; unusable text layers fall back to OCR (`unit/test_native_text.py`, `unit/test_extraction.py`)
- ✅ Tesseract via subprocess: TSV parsing, per-page timeout kills the process, missing engine / language / TSV output fails fast at worker start (`unit/test_ocr.py`, `docintel check-ocr`)
- ✅ Scans: low-DPI upscaling, projection-profile deskew, orientation correction of sideways pages, EXIF orientation, OCR token clean-up; bounded page parallelism; one failing page does not fail the document (`unit/test_ocr.py`, `unit/test_extraction.py`)
- ✅ Mixed PDFs handled per page (`unit/test_extraction.py::test_mixed_pdf_decides_per_page`, `integration/test_worker.py`)
- ✅ CER/WER measured on `synthetic-noisy` per degradation, with a preprocessing ablation (`evaluation/reports/ocr.md`)

Layout and tables
- ✅ Lines, column segments, reading-order blocks, label/value grids row by row, headings (`unit/test_layout.py`)
- ✅ Geometry-based tables with wrapped cells, row-rhythm stop, lost leading header, multi-page stitching (`unit/test_tables.py`); native line-item tables match ground truth exactly (`unit/test_tables.py::test_native_line_item_tables_match_ground_truth`, `evaluation/reports/tables.md`)
- ⏳ Scanned tables are partially recovered (row recall and cell accuracy in `evaluation/reports/tables.md`); Phase 4 extraction reads the page text and images as well, not only detected tables

Classification and AI gate
- ✅ Local calibrated classifier for nine types, trained at worker start from the synthetic corpus plus human corrections; deterministic fingerprint (`unit/test_classification.py`)
- ✅ LLM fallback behind the sensitivity gate; agreement-based confidence; hallucinated evidence penalized; provider errors degrade to review (`unit/test_classification.py`, `integration/test_understanding.py` with a fake provider)
- ✅ Content findings (payment cards, SSNs → RESTRICTED; resume/bank statement → CONFIDENTIAL) block external AI; values never stored (`unit/test_classification.py`, `integration/test_understanding.py::test_confidential_documents_never_reach_the_llm`)
- ✅ Classification metrics reported: accuracy, macro-F1, per class, confusion matrix, ECE, auto-accept error rate (`evaluation/reports/classification.md`) — on synthetic data only, stated in the report
- ⏳ LLM fallback against the real Gemini API: needs the user's key (`make check-ai`)

Persistence, API, UI
- ✅ Migration 0003 round-trips and matches the models (`integration/test_database.py`)
- ✅ Pages, preview images, tables, classification history and human correction via API; access control on every new endpoint; correction audited; human label survives reprocessing (`integration/test_understanding.py`)
- ✅ `REVIEW_REQUIRED` with reasons (`NO_TEXT_FOUND`, `CLASSIFICATION_UNCERTAIN`, `LOW_OCR_CONFIDENCE`, `OCR_FAILED`)
- ✅ UI: type column and filter, classification card with evidence and correction, page viewer with preview, word boxes and text, tables (`frontend/src/documents/documents.test.tsx`)

Delivery
- ✅ Dockerfile installs Tesseract (eng, deu, osd); worker verifies OCR before claiming jobs
- ⏳ The Dockerfile's `apt-get install` step itself was not run in the build environment (`deb.debian.org` blocked by its network policy); the stack was verified with the same Tesseract version supplied by a sandbox-only base-image shim. CI builds the real image
- ✅ CI: Tesseract installed for tests, evaluation suites smoke run, dataset ingest with `--require-completed` (dropped in Phase 4, where some synthetic documents are meant to need review); `actionlint` clean
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)

## 4. Phase 4 acceptance criteria

Status as of 2026-10-09, same legend as §1. Metrics live in `evaluation/reports/extraction.md`.

Schemas and extraction
- ✅ Eight versioned Pydantic schemas (invoice, purchase order, receipt, delivery note, contract, resume, bank statement, policy); every value cites page and quote; the model-facing JSON schema is self-contained (`unit/test_fields_scoring.py`)
- ✅ Layout extractor without any model: labels on the same line, below, across a skewed line break, OCR-damaged labels, letterhead issuer, table columns by header and content, section lists, derived currency; junk and summary rows dropped (`unit/test_fields_layout.py`, 18 tests)
- ✅ LLM extraction only when allowed and needed: `auto` skips the call when the layout result is confident; sensitive documents never reach an external model, a local model may see them; low-confidence OCR pages go as images; long documents are cut with a marker (`unit/test_fields_service.py`)
- ✅ Malformed model output gets one repair round-trip, then the layout result stands; provider errors degrade to the layout result; identical input reuses the stored output (`unit/test_fields_service.py::test_malformed_output_gets_one_repair_round_trip`, `::test_provider_errors_degrade_to_the_layout_result`, `::test_identical_input_reuses_the_stored_model_output`)
- ⏳ LLM extraction against the real Gemini API or a live Ollama server: needs the user's key / server (`make check-ai`); not measured

Evidence, normalization, validation, confidence
- ✅ Every value has provenance or is flagged: quotes located on the cited page (case/spacing-insensitive, word boundaries, fuzzy for OCR noise), wrong page citations recorded, hallucinated or unsupported values get confidence 0 (`unit/test_fields_layout.py`, `unit/test_fields_service.py::test_hallucinated_and_unsupported_values_get_no_confidence`)
- ✅ Normalization of amounts (decimal comma, digit grouping), currencies, dates with explicit day/month rules (ambiguous → `UNCERTAIN` until the document decides), terms, percentages, identifiers (`unit/test_fields_normalize.py`); every printed vendor-name variant resolves to its vendor; tax IDs and aliases decide (`unit/test_vendor_matching.py`)
- ✅ Consistency checks (line arithmetic, sums, tax, totals, due date, date order, balances); a printed arithmetic error is flagged, not hidden (`unit/test_fields_scoring.py`, `unit/test_fields_service.py::test_printed_arithmetic_error_is_flagged_not_hidden`)
- ✅ Confidence from measured factors; documents route to AUTO / analyst / mandatory review; a missing line-item table, or a row without its quantity or amount, blocks auto-acceptance (`unit/test_fields_scoring.py`, `unit/test_fields_service.py::test_a_missing_line_item_table_blocks_auto_acceptance_until_confirmed`, `::test_a_line_item_missing_an_essential_cell_is_not_auto_accepted`)
- ✅ Prompt injection: the document cannot close its data block; injected values never reach AUTO, whether they disagree with the layout reading or only the model reports them (`security/test_prompt_injection.py`)

Persistence, API, UI
- ✅ Migration 0004 round-trips and matches the models (`integration/test_database.py`)
- ✅ `/extraction`, `/evidence`, field correction and vendor endpoints with scope and permission checks; a reviewer's correction re-scores the document, is audited without values and survives reprocessing; a type correction re-extracts with the new schema (`integration/test_extraction_api.py`, `integration/test_understanding.py::test_uncertain_document_is_corrected_and_correction_survives_reprocessing`)
- ✅ LLM calls accounted in `llm_calls`; the daily budget holds across calls; cost only from configured prices (`unit/test_ai_providers_phase4.py`, `integration/test_extraction_api.py::test_llm_calls_are_accounted_and_the_daily_budget_holds`)
- ✅ Ollama provider: chat API request shape, schema format, images only for vision models, error mapping, retries, model listing — against a mocked server (`unit/test_ai_providers_phase4.py`)
- ✅ UI: extracted fields with evidence, confidence, ambiguity and competing reading; line items; consistency checks; "Show" outlines the source on the page preview; reviewer correction; vendor column (`frontend/src/documents/documents.test.tsx`); checked in headless Chromium against the Docker stack (inbox, a document with a wrong printed total, a scanned invoice, highlight, correction) with no console errors

Evaluation (synthetic data only, layout extractor without LLM, commit `44c14e2`)
- ✅ Field metrics measured: native PDFs 100% exact and normalized match (70 documents); re-rendered scans 95.9% exact, 99.3% normalized, F1 0.996; the dataset's own scans 100% normalized (4 documents)
- ✅ Line items: native rows and cells 100%; re-rendered scans row recall 0.738, cell accuracy 78.1%
- ✅ Routing: 97.1% of native documents auto-accepted with 0.0% error inside the auto bucket; no scanned document is auto-accepted (the first full run auto-accepted one scanned delivery note with a garbled table — fixed by ADR-033's essential cells)
- ✅ Consistency checks flag 4 of 4 printed arithmetic errors; 1.4% of correctly printed documents are flagged because a value was misread
- ✅ Printed vendor-name and date-format variants normalized correctly (8 of 8 documents)
- ⏳ LLM extraction quality, provenance page accuracy and bbox IoU: Not yet measured

Delivery
- ✅ 546 backend tests, ruff, ruff format, mypy --strict; 31 frontend tests, ESLint, `tsc`, production build; the full backend suite also passes in a fresh checkout (this caught the storage package that an unanchored `.gitignore` rule had kept out of git — and out of ruff, which skips ignored files — since Phase 2)
- ✅ Docker stack: `docker compose up --wait` healthy, smoke test passes, the synthetic dataset processes through the stack: 34 `COMPLETED`, 3 `REVIEW_REQUIRED` — the invoice with a wrong printed total (`EXTRACTION_INCONSISTENT`) and the two scanned documents of bundle B0012 (`EXTRACTION_UNCERTAIN`); built with the same sandbox-only base-image shim as Phase 3 (`deb.debian.org` blocked)
- ✅ CI: dataset ingest accepts `REVIEW_REQUIRED`; the quick evaluation includes extraction; gitleaks clean on history
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)
- ⏳ Cost per document: no prices are shipped (`LLM_PRICING`), and the Gemini pricing page was not reachable from the build environment

Not in Phase 4 (by design): comparison of documents against each other, near-duplicate detection, review tasks (Phase 5); calibration of the confidence factors on held-out data (Phase 10).
