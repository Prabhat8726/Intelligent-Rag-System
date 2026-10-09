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

### ADR-013 — Upload type is decided by content, and must agree with the name and declared type
* **Context**: extensions and `Content-Type` are attacker-controlled; a mislabelled file can reach parsers or be served back as active content.
* **Decision**: the file's magic bytes at offset 0 decide its kind; the extension must map to the same kind and the declared MIME type must be compatible (or generic). PDFs are opened with pdfium and images with Pillow (decompression-bomb guard, `verify()`) before anything is stored. Downloads are always `attachment` with `nosniff` and `CSP: sandbox`.
* **Consequences**: + polyglots and renamed files are rejected with 415/422 before storage (`tests/security/test_document_security.py`). − a legitimate file with a wrong extension must be renamed by the user.

### ADR-014 — Write the file first, then one database transaction, with a compensating delete
* **Context**: object storage and PostgreSQL cannot share a transaction.
* **Decision**: the validated file is stored under a fresh key (`documents/{id}/v{n}/original.{ext}`, never derived from the filename), then document, version, job and audit row commit together. If the transaction fails, the stored file is deleted.
* **Consequences**: + no database row ever points at a missing file. − a process crash between the two steps can leave an unreferenced file; it is harmless (unreachable key) and logged when deletion fails. A storage reconciliation job is planned with the retention work in Phase 11. Soft delete keeps the file (audit and retention); hard deletion is part of the same retention work.

### ADR-015 — Exact duplicates are accepted and flagged, within what the uploader can see
* **Context**: re-uploading the same file is often legitimate (resubmission, another department), but should be visible to reviewers. Telling a user "this file exists" for a document they cannot access would leak its existence.
* **Decision**: SHA-256 lookup restricted by the same access predicate as reads; a match sets `duplicate_of_id` + `duplicate_reason = EXACT_FILE_HASH` and the upload still succeeds. Near-duplicates (same invoice number, different bytes) are a Phase 5 rule.
* **Consequences**: + no cross-department existence oracle; duplicates are reviewable rather than lost. − the same file can be stored more than once (bounded by the upload limits).

### ADR-016 — Request size is limited twice
* **Context**: Starlette parses multipart bodies before route code runs, so a handler-level check alone would let a client stream unbounded data to disk.
* **Decision**: a pure-ASGI middleware rejects bodies over the limit by `Content-Length` and while streaming (`API_MAX_BODY_BYTES`, default 1 MiB; on `POST /documents` the limit is `UPLOAD_MAX_BYTES` + 1 MiB multipart overhead). The service enforces the exact file limit again while spooling.
* **Consequences**: + oversized requests are cut off early with a 413 problem response; the file limit is exact. − two places to keep consistent (both derive from `UPLOAD_MAX_BYTES`).

### ADR-017 — Per-page text-or-OCR decision during inspection
* **Context**: mixed PDFs (typed pages + scanned pages) are common; OCR on pages that already have text wastes time and lowers quality.
* **Decision**: the worker records, per page, character count, image objects and a method: no text → OCR; at least 20 characters → native text; fewer characters and no images → native text (a short typed page); fewer characters plus images → OCR. Stored in `document_versions.inspection`.
* **Consequences**: + Phase 3 OCRs only the pages that need it. − the 20-character threshold is a heuristic, checked against synthetic data (`tests/unit/test_inspection.py`); it gets measured on the noisy dataset in Phase 3.

### ADR-018 — pdfium access is serialized per process
* **Context**: pypdfium2 (pdfium) is not thread-safe, and validation and inspection run in worker threads.
* **Decision**: every pdfium call goes through one process-wide lock (`docintel.processing.pdf.PDFIUM_LOCK`).
* **Consequences**: + no native crashes from concurrent use. − PDF work is single-threaded per process; scale by adding worker processes (`WORKER_CONCURRENCY` overlaps I/O, not pdfium calls).

### ADR-019 — Synthetic dataset generator with recorded ground truth and defects
* **Context**: the prompt requires evaluation without real company data, and Gemini's free tier must not receive sensitive data (ADR-007).
* **Decision**: a seeded generator (reportlab, its own dependency group, not in the runtime image) produces linked PO / delivery note / invoice bundles for 12 scenarios (clean match, price/quantity/tax/total/vendor defects, duplicates, long multi-page, scanned) with per-document JSON ground truth that separates intended defects from harmless variations. `docintel ingest` uploads a dataset through the public API.
* **Consequences**: + reproducible inputs for every later metric (same seed → same bytes). − synthetic layouts are cleaner than real documents; real-world-like noise is added only for the scanned scenario for now.

### ADR-020 — Client-side CLI commands do not load server configuration
* **Context**: `generate-documents` and `ingest` run on a developer machine or in CI against a running stack; requiring `DATABASE_URL`/`JWT_SECRET_KEY` for them would force secrets onto machines that do not need them.
* **Decision**: the CLI dispatches these commands before reading settings; `ingest` takes only an API URL and credentials (`--password-stdin` or `SEED_USER_PASSWORD`) and exits non-zero when a document fails, is rejected or does not finish (`REVIEW_REQUIRED` counts as processed unless `--require-completed`, which CI uses).
* **Consequences**: + the dataset tooling works anywhere, and CI fails when any document fails. − two code paths in `main()` (covered by `tests/integration/test_seed_and_cli.py`).

### ADR-021 — One PDF library and one geometry-based layout path for native and OCR text
* **Context**: the plan used pdfplumber (pdfminer.six) for native words and tables, with a vision model for tables on scans.
* **Decision**: native words come from pypdfium2 (already used for validation and rendering); table detection runs on word geometry, so the same code handles native and OCR pages.
* **Consequences**: + one parser of untrusted PDFs instead of two; identical coordinates for previews, words and tables; native line-item tables are exact on the synthetic set (`evaluation/reports/tables.md`). − heuristic thresholds (column gap 0.75 × text size, row rhythm); tables without a header row of short labels are not detected; scanned tables lose rows (measured, see the report).

### ADR-022 — The local classifier is trained at worker start, not shipped as a file
* **Context**: a pickled scikit-learn model is code execution on load and drifts from the code that built it; a separately trained artefact needs its own release process.
* **Decision**: the worker trains the TF-IDF + logistic-regression model from the seeded synthetic corpus plus current human corrections (`document_classifications`, method HUMAN) at start-up, and records a fingerprint of the training data and library versions with every prediction.
* **Consequences**: + nothing to unpickle; corrections take effect at the next worker start; predictions are traceable to a training set. − 9–20 s worker start-up; two workers started at different times may use different correction sets (visible in the fingerprint).

### ADR-023 — OCR preprocessing is chosen by measurement
* **Context**: OCR advice (binarize, denoise, remove lines, deskew) often hurts as much as it helps.
* **Decision**: each step is an option evaluated by the OCR suite; only steps that improved the synthetic scans are on by default: upscaling low-DPI images, projection-profile deskew, token clean-up, orientation retry. Median filter, autocontrast and ruling-line removal are off (or absent).
* **Consequences**: + defaults are justified by numbers in `evaluation/reports/ocr.md`. − measured on synthetic scans only; real scanners may need re-measurement (the suite can be rerun with other data later).

### ADR-024 — Vision fallback moves to Phase 4 extraction
* **Context**: the plan sent low-confidence OCR pages to a vision model in Phase 3.
* **Decision**: Phase 3 flags such pages (`LOW_OCR_CONFIDENCE`); Phase 4 extraction attaches the page images of flagged pages to its (sensitivity-gated) LLM call.
* **Consequences**: + one external call per document instead of two, and the image is used where the value is (field extraction). − until Phase 4, pages that OCR reads poorly are only flagged for review.

### ADR-025 — Review reasons on the document, not a review table yet
* **Context**: processing must say why a document needs a person; the review-task workflow arrives in Phase 5.
* **Decision**: `documents.review_reasons` (JSON list of codes: `CLASSIFICATION_UNCERTAIN`, `NO_TEXT_FOUND`, `LOW_OCR_CONFIDENCE`, `OCR_FAILED`); a non-empty list sets `REVIEW_REQUIRED`. A human type correction clears `CLASSIFICATION_UNCERTAIN`; the document completes when nothing else is open.
* **Consequences**: + reasons are visible in the API and UI now. − Phase 5 converts them into review tasks (assignment, SLA).

### ADR-026 — Content-based sensitivity can only raise the level used for AI routing
* **Context**: users mislabel documents; some content must never reach an external model.
* **Decision**: payment card numbers (Luhn) and US SSNs imply RESTRICTED; resumes and bank statements imply CONFIDENTIAL; the gate uses the maximum of these and the uploaded label. The document's own label is not changed; findings store counts and pages, never values.
* **Consequences**: + safe by construction for the patterns covered. − pattern coverage is narrow (no names/addresses detection); IBANs and e-mails are recorded but do not raise the level because they appear on ordinary invoices.

### ADR-027 — Evaluation reports are committed under stable names
* **Context**: README numbers must trace to a recorded run; the plan suggested timestamped files.
* **Decision**: `docintel evaluate` writes `evaluation/reports/<suite>.json|.md` with commit (marked `+dirty` for uncommitted trees), versions, seeds and configuration; git history keeps older runs. CI runs the suites with small datasets as a smoke test.
* **Consequences**: + one place to look, diffs show metric changes in review. − full runs take ~10 minutes and are run by a developer, not by CI.

### ADR-028 — Layout rules extract first; the LLM is a second, checked reader
* **Context**: the free tier limits requests, some documents may not leave the deployment, and a model can be talked into values by the document's own text.
* **Decision**: a deterministic layout extractor (labels, letterhead, table headers, section lists) runs on every document. The LLM runs only when allowed by the sensitivity gate and, in the default `EXTRACTION_LLM_MODE=auto`, only when the layout result would not be auto-accepted. Both outputs are merged field by field: agreement raises confidence; on disagreement the value the vendor master recognizes, else the better-evidenced value, else a strong layout rule wins, and the other value is kept for the reviewer. No value is accepted without being found on the page.
* **Consequences**: + extraction works with no key and no network, most clean documents cost zero calls, and with the default thresholds a value the model alone reports (the target of an injected instruction) is never auto-accepted: disagreement scores 0.6 and a model-only value 0.8. − the layout rules know the label vocabulary of the synthetic templates and common business wording; unfamiliar layouts depend on the LLM or a reviewer. The LLM path is not yet measured (no key in the build environment).

### ADR-029 — A self-hosted model provider is not "external AI"
* **Context**: the sensitivity gate (ADR-026) blocks content above `AI_EXTERNAL_MAX_SENSITIVITY` from leaving the deployment; Phase 4 adds Ollama as a local `LLMProvider`.
* **Decision**: providers declare `local`; the gate allows a local provider for any sensitivity and records the reason ("local model: content stays in the deployment"). Gemini is never local.
* **Consequences**: + confidential and restricted documents can use model extraction on a self-hosted model. − "local" means the operator's Ollama endpoint; pointing `OLLAMA_BASE_URL` at a remote host moves content out, so the setting is documented as an operator responsibility.

### ADR-030 — Field confidence is a product of measured factors; documents route on the weakest field
* **Context**: Module 26 needs a confidence that drives routing; model self-reports are not evidence (ADR-005).
* **Decision**: field confidence = product of factors for evidence, normalization, OCR quality, layout rule strength, conflicts, citation page, consistency checks and source agreement (`fields/confidence.py`); a value only the LLM read gets 0.8, below the AUTO threshold. Document confidence = minimum over required fields and line-item cells. AUTO needs ≥ 0.85 and no failed check; ≥ 0.6 is analyst review; below is mandatory review.
* **Consequences**: + every number is explainable from stored signals and recomputed after a correction. − the factor values are design choices, not fitted; the evaluation reports the error rate inside the auto-accepted bucket, and Phase 10 calibrates on held-out data.

### ADR-031 — Every LLM call is accounted; cost needs configured prices
* **Context**: Module 43 requires usage and cost tracking; published prices change and the pricing page could not be fetched from the build environment.
* **Decision**: a provider decorator records each call in `llm_calls` (provider, model, purpose, document, prompt version, tokens, latency, status; never prompts or outputs) and enforces `LLM_DAILY_REQUEST_BUDGET` before sending. Cost is computed only from `LLM_PRICING` (per-million-token prices the operator enters); none are shipped, and a local model costs 0.
* **Consequences**: + no invented prices in reports; usage is visible via `docintel llm-usage`. − cost shows as unknown until prices are configured.

### ADR-032 — Corrections are values as printed, re-scored, kept on reprocessing, audited without values
* **Context**: reviewers fix extracted values; reprocessing must not silently drop their work; audit logs must not copy document content.
* **Decision**: a correction is the text as printed (empty = not on the document); it is normalized with the document's context, scored as human evidence, and the document's review reasons are recomputed. Reprocessing the same version with the same schema re-applies corrections. A type correction that changes the schema re-extracts with the new schema and retires the old extraction. Audit events record field, document and actor, not values.
* **Consequences**: + corrections survive reprocessing and feed routing immediately. − corrections do not yet train the layout rules (Phase 10 uses them as labelled data).

### ADR-033 — Line-item documents require table rows, and rows require their essential cells
* **Context**: the first full extraction evaluation auto-accepted scanned delivery notes whose header fields were right but whose table was not found; a later run auto-accepted one whose OCR had garbled the table into a single row without quantity, because routing only weighed cells that existed.
* **Decision**: invoices, purchase orders, delivery notes and bank statements have a required row-count field; no rows means a missing required field. A reviewer confirms a document without rows with an empty correction. Columns marked essential (quantity; amount for priced lines) get an explicit `NOT_FOUND` cell when a row lacks them, with confidence 0.
* **Consequences**: + a missed table or a row without its quantity can no longer be auto-accepted, and the reviewer sees exactly which cell is missing. − documents that genuinely have no table, or service lines without a quantity, always need one review. A table read with too few rows but complete ones is not detected by this rule; the error rate inside the auto bucket is reported to watch for it.
