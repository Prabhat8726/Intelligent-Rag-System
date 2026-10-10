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
| **5 — Comparison & rules** ✅ | Comparison engine, rule engine, duplicates, versions diff, review queue | `/comparisons`, `/rules`, `/review-tasks`, `/documents/{id}/findings`, `/versions`, contract version diff | Discrepancy P/R/F1 on scenarios; rules configurable; CI regression suite |
| **6 — Knowledge & RAG** ✅ | KB ingestion, chunking, embeddings, hybrid retrieval, citations, semantic search, fastembed local provider | `/knowledge/*`, `/search`, `make seed-knowledge`, `docintel reembed` | Retrieval metrics measured with ablations; access filters proven by tests |
| **7 — Agent** ✅ | LangGraph graph, tool registry, planner, guardrails, MCP server, API tokens | `/analysis`, `mcp` entrypoint | Agent scenario success/tool-selection measured; injection tests pass |
| **8 — Workflow automation** ✅ | Workflows, HITL state machine, executors, reports, audit API, user management | `/workflows/*`, `/reports`, `/audit-logs`, `/users` | Maker-checker enforced; transitions audited; reports reproducible |
| **9 — Frontend** ✅ | Dashboard, inbox, viewer with evidence highlights, comparison, AI analysis, approvals, audit, search; refresh tokens | Full SPA, `/dashboard/summary`, `/auth/refresh`, `/auth/logout`, `make e2e` | E2E (Playwright) for the demo path |
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

## 5. Phase 5 acceptance criteria

Status as of 2026-10-09, same legend as §1. Metrics live in `evaluation/reports/discrepancies.md`
and `versions.md`.

Comparison and duplicates
- ✅ Invoice ↔ purchase order ↔ delivery notes (two- and three-way, delivered quantities summed over notes) and delivery note ↔ order; lines paired by item code then description; MATCH / MISMATCH / MISSING / UNCERTAIN with difference, tolerance and evidence from both documents; differing currencies, weak readings, misread item codes and documents without readable lines are UNCERTAIN (`unit/test_matching.py`)
- ✅ Duplicates: same vendor and number (strong), vendor + amount + date within the window (possible), byte-identical files; only older documents are originals (`unit/test_matching.py`, `unit/test_rules.py`, `integration/test_matching_api.py::test_a_resent_invoice_is_held_as_a_duplicate_until_the_original_goes`)
- ✅ Matching runs in the processing transaction under a per-department lock and re-evaluates related documents, so arrival order does not matter (`integration/test_matching_api.py::test_an_invoice_is_matched_when_its_order_arrives_later`); never across departments (`security/test_matching_security.py`)

Rules
- ✅ 19 default rules seeded by migration 0005 with typed parameters (`extra=forbid`); a broken rule reports ERROR and the others still run; disabled rules do not run; tolerances come from the rules (`unit/test_rules.py`)
- ✅ Every planted discrepancy in generated bundles, extracted from the real PDFs, fails exactly its rule — as a FAIL, not a warning — and clean bundles pass (`unit/test_rules.py::test_generated_bundles_raise_exactly_their_discrepancy`, `::test_a_resent_invoice_is_a_duplicate`)
- ✅ Rules readable by every role, changed only by administrators (validated, versioned, audited before/after), re-evaluated on request (`integration/test_matching_api.py::test_rules_are_read_by_all_changed_by_admins_and_re_evaluated`)

Review queue
- ✅ One open task per document; reasons with stable keys, priority from severity, SLA due dates; claim, release, override only by managers and admins; approve / correct / reject (note required); accepted findings stay closed for the version, new findings reopen; tasks clear by themselves; a new version cancels the old task; viewers cannot work the queue (`integration/test_matching_api.py::test_review_lifecycle`, `::test_a_manager_takes_over_a_claimed_task_and_a_corrected_version_supersedes_it`, `unit/test_review_items.py`)
- ✅ Migration 0005 round-trips, matches the models, seeds the 19 rules and opens tasks for documents already in review (`integration/test_database.py::test_phase5_migration_opens_tasks_for_documents_waiting_for_review`)

Versions
- ✅ New versions uploaded with the same validation; 409 while processing or for an identical file; clause segmentation and alignment by title, number and text; renumbering is not a change (`unit/test_versions.py`, `integration/test_matching_api.py::test_contract_versions_are_compared_clause_by_clause`)

Frontend
- ✅ Review queue, comparison view with evidence links that open the field on its page, rules (read-only and admin editing with JSON validation), document findings with claim/resolve, versions with upload and clause diff, inbox priority (`frontend/src/review/review.test.tsx`, `frontend/src/documents/documents.test.tsx`)

Evaluation (synthetic data only, layout extractor without LLM, commit `fa1fcce`)
- ✅ Discrepancy P/R/F1 measured per defect type, per rule and over (document, rule) pairs: as generated FAIL precision and recall 100% (36 planted findings, 148 documents), no false FAIL; re-rendered scans FAIL 80.0% / 77.8%, FAIL-or-warning recall 94.4% at 45.3% precision
- ✅ Resent invoices: 4 of 4 found, no wrong pair (native and scanned)
- ✅ Contract versions: 40 of 40 steps exactly right, native and scanned; segmentation 100%
- ✅ The suites changed the code: they exposed a reading-order bug in OCR text (a line tail read after its paragraph), false failures from documents without readable lines and from misread item codes, an arithmetic rule that discounted values for the very check they failed, and billed lines on no delivery note that no rule caught — each fixed with a regression test

Delivery
- ✅ 611 backend tests, ruff, ruff format, mypy --strict; 47 frontend tests, ESLint, `tsc`, production build
- ✅ Docker stack: `docker compose up --wait` healthy, smoke test passes (an invoice without its order on file is held for review with `INV_MISSING_PO`), the synthetic dataset processes through the stack with every planted discrepancy in the review queue under its rule; built with the sandbox-only base-image shim (`deb.debian.org` blocked)
- ✅ CI: the quick evaluation includes both new suites; gitleaks clean on history; `actionlint` clean
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)
- ⏳ Rule evaluation with LLM-extracted values (no key or local model in the build environment)

Not in Phase 5 (by design): contract ↔ policy and resume ↔ job comparison (agent tools, Phase 7); workflow actions on review decisions (Phase 8); calibration of the confidence threshold on real documents (Phase 10).


## 6. Phase 6 acceptance criteria

Status as of 2026-10-09, same legend as §1. Metrics live in `evaluation/reports/retrieval.md`
and `search.md` (commit `42fedf9`).

Knowledge base (Module 12)
- ✅ Markdown, text, PDF and image uploads with front-matter or form metadata; UTF-8/binary/size/front-matter validation; scope organization-wide or one department (managers: own department, administrators: any); a version can never change its audience; duplicates rejected (`unit/test_knowledge_ingestion.py`, `integration/test_knowledge_api.py`)
- ✅ Worker: integrity, parse (PDFs through the business-document extraction), section-aware chunking with breadcrumbs, content sensitivity scan, gated embeddings; embedding failures retried, then stored full-text only with a note (`integration/test_knowledge_api.py`)
- ✅ Versions: newer ACTIVE, older SUPERSEDED in either upload order and with concurrent uploads; retrieval windows on chunks; archiving the active version restores the previous one (`test_knowledge_api.py::test_a_new_version_supersedes_the_old_one_in_either_upload_order`, `::test_archiving_the_active_version_restores_the_previous_one`)
- ✅ Business documents indexed by the pipeline's `index` stage, removed from the index on delete; `docintel reembed` adds vectors of the configured model and leaves gated chunks alone (`test_knowledge_api.py::test_business_documents_are_indexed_and_removed_on_delete`, `::test_reembed_adds_vectors_of_the_configured_model`)

RAG (Module 13)
- ✅ Hybrid retrieval with filters in SQL (access, status, window, category, keys); deterministic results (two runs of the suite identical) (`integration/test_knowledge_rag.py`)
- ✅ Citations: answers composed from claims citing provided sources, invalid citations removed, numbers and words checked against the cited text, statuses ANSWERED / PARTIALLY_SUPPORTED / INSUFFICIENT_EVIDENCE / RETRIEVAL_ONLY; unanswerable questions refused without a model call; queries audited with a question fingerprint and model calls accounted (`test_knowledge_rag.py`, `unit/test_rag.py`)
- ✅ Superseded policy cited only for past dates; the Legal playbook invisible to Finance on every path and never sent to an external model; planted instructions stay inside the delimited sources; hostile query strings are text or 422 (`security/test_knowledge_security.py`)

Search (Module 28)
- ✅ "Invoices from Vendor X", "contracts containing termination clauses", "payment terms longer than 60 days" (extracted, or read from text), totals, dates, free text with snippets; department-scoped (`integration/test_document_search.py`, `unit/test_search_query.py`)

Evaluation (lexical hashing embeddings, synthetic data)
- ✅ Retrieval with ablations: hybrid MRR 0.938 on kb-queries (tuning) and 0.950 on the holdout; full text 0.918, dense 0.885, no prefix 0.812, fixed-size chunks 0.841; gate refused 5/6 and 2/4 unanswerable questions, 1/48 and 0/10 false refusals; 0 passages of another department, 0 superseded passages as of the evaluation date
- ✅ Search: 100% precision and recall on 26 structured questions; text recall@10 100%
- ✅ The suites changed the code: non-deterministic tie-breaking (fixed), the full-text order chosen per mode on kb-queries, vendor names containing "and" (fixed), versions of one document rejected while one was processing (allowed now), a NUL byte in a query causing a 500 (now 422)
- ⏳ Gemini and fastembed embeddings, and generated answers (citation precision/recall, faithfulness) — no key or model download in the build environment

Delivery
- ✅ 733 backend tests, ruff, ruff format, mypy --strict; 58 frontend tests, ESLint, `tsc`, production build
- ✅ Docker stack rebuilt (sandbox-only base-image shim: `deb.debian.org` blocked), migration 0006 applied by the migrate service, smoke test passes on a fresh stack, the synthetic dataset (37 documents) and the knowledge base (11 files: 10 ACTIVE, 1 SUPERSEDED) load through nginx, knowledge and search APIs checked as Finance and Legal users, browser check of the Knowledge and Search pages without console errors
- ✅ gitleaks clean on history; `actionlint` clean; CI loads the knowledge base through the stack
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)

Not in Phase 6 (by design): agent use of retrieval and policy explanations of rule results (Phase 7); reranking (only if measured to help, Phase 10); calibrating `RAG_MIN_DENSE_SIMILARITY` for Gemini or fastembed (needs those models).


## 7. Phase 7 acceptance criteria

Status as of 2026-10-10, same legend as §1. Metrics live in `evaluation/reports/agent.md`
(commit `b5d2ab7`).

Agent workflow (Module 14)
- ✅ LangGraph `StateGraph` with explicit, JSON-compatible state: understand request → identify documents → inspect extraction → run rules → (compare documents) → retrieve knowledge → analyse (one bounded follow-up round) → determine confidence → recommend → approval gate → request a review / propose for approval → finish (`agent/graph.py`, `integration/test_agent_analysis.py`)
- ✅ Runs in the worker as `AGENT_ANALYSIS` jobs (one attempt), plan, result, trace, tool calls and model usage stored; audited as AGENT on behalf of the requester, the request by fingerprint (`test_agent_analysis.py::test_a_price_mismatch_is_investigated_and_sent_for_review`)
- ✅ Deterministic without a model; with one, the model only plans (typed, bounded) and writes findings that must cite existing evidence, keep to its numbers and never clear a failed rule; guardrails overrule its proposals (`::test_model_findings_are_validated_and_guardrails_overrule_the_model`, `unit/test_agent.py`)
- ✅ Content above the external AI limit is not sent to the model (`::test_content_above_the_sensitivity_limit_is_not_sent_to_the_model`)
- ✅ Recommendations from an allowlist with a risk table: payment and duplicate rejection proposed for a manager, vendor clarification for a reviewer, review requests executed only when allowed (`::test_clean_invoices_are_proposed_for_payment_and_duplicates_for_rejection`)

Agent tools (Module 15)
- ✅ search_documents, get_document, get_extracted_fields, get_document_evidence, search_knowledge_base, compare_documents, run_business_rules (dry run), create_review_task (review requests that survive re-evaluation until resolved): typed inputs and outputs, permissions narrowed by token scopes, validation, safe errors, timeouts, output caps and a log row per call (`integration/test_agent_tools.py`)
- ✅ generate_report and get_workflow_status — delivered in Phase 8 (§8)

MCP (Module 16)
- ✅ `docintel mcp` over stdio and streamable HTTP, the same registry and schemas; personal API tokens (hashed, scoped, expiring, revocable, re-checked per call); DNS-rebinding protection; calls logged and audited (`integration/test_mcp.py`)

Security
- ✅ Personal runs; no access to other departments' documents by id, search or rules; hostile requests cannot escape their markers, choose tools or pass the guardrails; tool and time budgets (`security/test_agent_security.py`)

Evaluation (deterministic mode, synthetic data)
- ✅ Development dataset (seed 7, 70 runs) and held-out dataset (seed 11, 60 runs, run only after development): task success 100% named, 100% found from the question, 100% policy questions (development); 0 unsafe recommendations; planted defect reported 100%; 0 false failures on clean invoices; tool selection precision 99.3% / 99.2%, recall 100%; every finding's evidence exists; governing policy among the sources 85.7% (vendor mismatches missed with lexical embeddings); scripted adversary: 0 payment recommendations, 0 statements or summaries kept
- ✅ The suite changed the code: identification by document number (exact matches only), the keyword planner (questions without a document go to the knowledge base, "Which …?" questions, team names read as vendors) and a guardrail (clearing an issue while a rule fails)
- ⏳ Model-assisted planning and analysis (Gemini or a local model) — no model in the build environment

Frontend
- ✅ AI analysis pages (start, follow, findings with evidence, sources, confidence, recommendation, action, steps and tool calls), Investigate from a document, API tokens page (`analysis/analysis.test.tsx`)

Delivery
- ✅ 778 backend tests, ruff, ruff format, mypy --strict; 62 frontend tests, ESLint, `tsc`, production build
- ✅ Docker stack rebuilt (sandbox-only base-image shim), migration 0007 applied by the migrate service, smoke test passes on a fresh stack; browser check on the stack with the synthetic dataset and knowledge base: an invoice investigated from its document page (vendor mismatch held for review, review requested, policy sources shown), a policy question answered from the knowledge base, the investigations list, a token created, shown once and revoked, no start form for a viewer, no console errors
- ✅ MCP against the stack's database with a personal token (documents and knowledge scopes), over stdio and streamable HTTP: only the six tools the scopes allow are listed; search, fields, rules and knowledge calls succeed; `create_review_task` denied, an invalid id refused; anonymous and forged tokens 401, a foreign Host header 421; the token revoked afterwards
- ✅ gitleaks clean on history; `actionlint` clean
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)

Not in Phase 7 (by design): approving and executing proposed actions (HITL state machine, workflow actions — Phase 8), reports, contract ↔ policy and resume ↔ job comparisons as agent tools (need extraction ground truth for those types).


## 8. Phase 8 acceptance criteria

Status as of 2026-10-10, same legend as §1. Metrics live in `evaluation/reports/workflow.md`.

Workflows (Module 18)
- ✅ Invoice processing (check → investigate → propose → approval → execute → report) and contract review (adds the comparison with the previous version), code-defined and versioned, run by the worker as `WORKFLOW` jobs; one active workflow of a type per document; start validation (processed, right type, visible), cancel while queued or running (`integration/test_workflows.py::test_starting_needs_a_processed_document_of_the_right_type`)
- ✅ Clean invoice → payment proposed for a manager → approved by someone else → executed with a payment reference, report generated, every step audited (`::test_a_clean_invoice_is_approved_for_payment_by_someone_else`); duplicate → rejection proposed, the approver can say no and the document returns to the review queue (`::test_a_duplicate_is_proposed_for_rejection_and_the_approver_can_say_no`); discrepancy → held for review at once (`::test_a_discrepancy_goes_to_review_without_waiting_for_approval`); a model proposal to ask the vendor waits for a reviewer and drafts the letter (`::test_a_model_proposal_to_ask_the_vendor_waits_for_a_reviewer`)
- ✅ Contract review against the Contract Management Guidelines (required clauses, notice ≤ 90 days, Ohio law, expiry) and the previous version; approval held while a review task is open; a proposal made stale by a new version cannot be approved (`::test_contract_review_against_the_guidelines_and_previous_versions`)
- ✅ Automatic start per type (`WORKFLOW_AUTO_START`), once per version, as the uploader (`::test_workflows_start_when_a_document_is_processed`)

Human approval (Module 17)
- ✅ Action states PROPOSED → AWAITING_APPROVAL → APPROVED / REJECTED → EXECUTED / FAILED; transitions validated, written with the user, time, reason and an audit event; history append-only (`unit/test_workflows.py::test_the_transition_table`, `integration/test_workflows.py::test_maker_checker_holds_for_managers_and_in_the_database`)
- ✅ Maker-checker: starter, owner and uploader cannot decide (service 403, audited; database CHECK); role per risk; rejection needs a reason (`::test_maker_checker_holds_for_managers_and_in_the_database`, `security/test_workflow_security.py`)
- ✅ Executors re-check the current data: an approval of an invoice that became defective fails with the reason (`::test_an_approval_fails_when_the_invoice_no_longer_qualifies`); a hijacked model cannot get a defective invoice paid (`security/test_workflow_security.py::test_a_hijacked_model_cannot_get_a_defective_invoice_paid`)

Reports (Module 30), audit and users
- ✅ Invoice verification, contract review, compliance review, document comparison and AI analysis reports: snapshot + Markdown + SHA-256, as-of from the data, regenerating unchanged data gives the same hash, verify re-renders, JSON and Markdown downloads audited, visible only to readers of all its documents (`integration/test_reports.py`, `unit/test_workflows.py::test_rendering_is_deterministic_and_escapes_document_text`)
- ✅ `GET /audit-logs` with filters and keyset paging, scoped to the department for managers (`integration/test_admin_api.py::test_the_audit_trail_is_scoped_to_the_department`); user and department administration with lock-out guards and token revocation (`::test_administrators_manage_users`, `::test_administrators_cannot_lock_themselves_or_everyone_out`, `security/test_workflow_security.py::test_nobody_escalates_through_the_admin_api`)
- ✅ Agent tools generate_report and get_workflow_status (`integration/test_reports.py::test_report_and_workflow_tools`); like every registry tool they are served to MCP clients, with the token scopes `reports:create` and `workflows:read` added

Evaluation (deterministic mode, synthetic data)
- ✅ Invoice processing, development (seed 7) and held-out (seed 11), 22 workflows each: final proposal as expected 100%, 0 unsafe proposals, every action executed; contract review, 12 families × 3 versions (seed 73): 36 of 36 proposals as expected, contract rule outcomes 144 of 144, version comparison step exactly right on 24 of 24 steps, 4 approvals first held for an open review task
- ✅ Maker-checker: 120 of 120 attempts refused (service and direct table write), 0 bypasses, every refusal audited; 276 of 276 action state changes audited; every workflow report re-renders to its hash and regenerating gives the same SHA-256
- ✅ The phase changed the data: the contract generator records its guideline ground truth, and half of its contracts now use the company's law with some notice periods above 90 days, so both outcomes are covered (versions suite unchanged: 40 of 40 steps)
- ⏳ Model-assisted proposals (Gemini or a local model) — no model in the build environment

Frontend
- ✅ Workflows (awaiting my decision / all, detail with steps, investigation, proposed action, why you cannot decide, decision with reason, history), reports (list, content, verify, downloads, generate from a document, comparison or investigation), audit log (filters, paging), users (create, edit, deactivate, reset password), the document page's workflows and reports, a badge with the decisions waiting (`workflows/workflows.test.tsx`)

Delivery
- ✅ 824 backend tests, ruff, ruff format, mypy --strict (src and tests); 68 frontend tests, ESLint, `tsc`, production build
- ✅ Docker images rebuilt (sandbox-only base-image shim); on a fresh stack migration 0008 applies (schema at `0008`, contract rules seeded) and the smoke test passes; the existing stack upgraded in place
- ✅ Verification found and fixed: the worker's container probe imported the whole CLI (scikit-learn, LangGraph, the workflow engine) and took about 5 s against a 5 s timeout, so a busy host marked the worker unhealthy; compose now runs `python -m docintel.workers.health` (about 1 s including `docker exec`; a test keeps the processing and agent stacks out of it); `mypy` errors in two test files
- ✅ Browser check on the stack with the synthetic dataset: an analyst starts invoice processing from a document and sees why they cannot decide; the manager's badge and "Awaiting my decision" list it; the manager approves with a reason (payment reference, history of four transitions, report verified against its hash); a duplicate's rejection proposed and approved by the manager; an administrator filters the audit log and creates a user; the manager sees the department's audit log and no Users page; a viewer reads a workflow without a decision form; no console errors
- ✅ gitleaks clean on history; `actionlint` clean
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)

Not in Phase 8 (by design): connections to external systems (ERP payment, e-mail, e-signature — executors record decisions only), PDF reports, workflow definitions edited at run time, escalation and reminders for decisions waiting too long.

## 9. Phase 9 acceptance criteria

Status as of 2026-10-10, same legend as §1.

Browser sessions (refresh tokens, ADR-063)
- ✅ The web login (`X-Docintel-Session: 1`) sets an httpOnly, `SameSite=Strict` refresh cookie scoped to `/api/v1/auth`, never in a body, stored as SHA-256; API clients get no cookie (`integration/test_sessions.py::test_the_web_app_gets_an_httponly_cookie_and_api_clients_do_not`)
- ✅ Every refresh rotates the cookie within one family (`::test_refresh_rotates_the_cookie_and_returns_a_working_token`); a replayed cookie revokes the family and is audited as `auth.refresh_reused` (`::test_a_replayed_cookie_ends_the_whole_session`); a refresh response lost to a reload is not theft, and its orphan presented later is (`::test_a_lost_refresh_response_is_not_theft`, `::test_the_grace_window_ends_and_a_used_successor_closes_it`)
- ✅ Logout (CSRF header required, no alarm), idle expiry, password reset and deactivation end sessions (`::test_logout_revokes_the_session_without_an_alarm`, `::test_refresh_needs_the_header_and_a_live_cookie`, `::test_deactivation_and_password_reset_end_sessions`); access tokens default to 15 minutes
- ✅ The SPA keeps the token in memory, restores it after a reload, renews a rejected token once, renews a minute before expiry, signs out every tab (`src/app.test.tsx`)

Dashboard and inbox (Module 19, ADR-064)
- ✅ `GET /dashboard/summary?days=1..90`: documents by status and type, processing time (mean, p95) and failures, review queue (open, overdue, priority, type), documents failing each rule, investigations (personal; administrators all), workflows awaiting approval and outcomes, extraction confidence and auto-acceptance per UTC day, recent activity from an allowlist of audit actions on visible documents; nothing from another department (`integration/test_dashboard.py`)
- ✅ Dashboard page: key figures linking to the inbox and queues, confidence trend and documents per day, bars for status, type, failing rules, outcomes and recommendations, each chart with a table view and a keyboard readout, period selector, recent activity (`src/dashboard/dashboard.test.tsx`); the inbox shows each document's extraction confidence and review level and opens filtered from the dashboard (`src/documents/documents.test.tsx`)
- ✅ Viewer with evidence highlights, comparison, AI analysis, approvals, audit and search were delivered with their back ends (Phases 4–8) and are part of the demo path below

Demo path end to end (master prompt §50, ADR-066)
- ✅ `frontend/e2e/demo.spec.ts` in Chromium against the Docker stack: a fresh order, delivery note and invoice with a planted price difference (`generate-documents --scenario UNIT_PRICE_MISMATCH`), uploaded by the analyst, classified, extracted, compared; the mismatch and the comparison shown; invoice processing retrieves the invoice processing procedure and proposes asking the vendor; the analyst is shown why they cannot decide; the reviewer approves, the drafted vendor message and the verified report appear; the administrator's audit log shows `workflow.action.approved` by the reviewer; the dashboard after a reload lists the activity and the failing rule. `make e2e`; CI runs it in the containers job and keeps the trace on failure
- ✅ A confirmed price or tax-rate difference (and nothing else) is put to the vendor as the invoice processing procedure says, with a reviewer's approval; quantity, missing order, total and vendor problems are still held (ADR-065; `integration/test_agent_analysis.py::test_a_price_mismatch_is_investigated_and_put_to_the_vendor`, `::test_a_quantity_difference_is_held_and_sent_for_review`, `integration/test_workflows.py::test_a_price_difference_is_put_to_the_vendor_once_a_reviewer_approves`)

Evaluation (deterministic mode, synthetic data)
- ✅ Agent and workflow suites re-run from a clean checkout after the vendor-clarification change: price and tax-rate mismatches now end in REQUEST_VENDOR_CLARIFICATION (agent: 8 of 8 per dataset; workflows: awaiting the vendor after a reviewer's approval), every expectation still met on the development and held-out datasets, 0 unsafe recommendations or proposals; maker-checker 152 of 152 refused, 0 bypasses; 284 of 284 action state changes audited
- ⏳ Model-assisted runs (Gemini or a local model) — no model in the build environment

Delivery
- ✅ 837 backend tests, ruff, ruff format, mypy --strict (src and tests); 74 frontend tests, ESLint, `tsc`, production build
- ✅ Docker images rebuilt (sandbox-only base-image shim); on a fresh stack migration 0009 applies and the smoke test passes; the end-to-end test passes on the fresh stack (knowledge base loaded from scratch) and on the upgraded long-lived stack
- ✅ Verification found and fixed: a reload that aborted a refresh after the cookie rotated looked like theft and signed the user out (grace window, ADR-063); nginx resolved the API once at start-up, so a recreated API container got 502s until nginx restarted (now resolved per request, cached 10 s; checked by recreating only the API); an anonymous visitor's start-up refresh produced a 401 in the console (the app now asks only when this browser signed in before); at phone width (390 px) the header overlapped and nine screens scrolled sideways (the header wraps, tables scroll inside their cards, filter and action rows wrap: no screen overflows); "1 workflows finished"
- ✅ Browser check on the stack: the dashboard at 1366 px and 390 px (screenshots reviewed); at 390 px also the inbox, review queue, workflows, reports, AI analysis, knowledge, search, rules, audit log, users, a document and a workflow; no horizontal page scroll, no console errors
- ✅ gitleaks clean; `actionlint` clean
- ⏳ CI run on GitHub — happens on the first pull request (or manual `workflow_dispatch`)

Not in Phase 9 (by design): a dark theme (the app is light-only), editable dashboards or saved views, notifications, and browser tests beyond the demo path (component tests cover the screens).
