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

### ADR-034 — Matching runs inside the processing transaction, serialized per department
* **Context**: an invoice, its order and its delivery notes arrive in any order and are processed by concurrent workers; a comparison computed from half-committed state would be wrong or missing, and two runs locking the same rows in different orders deadlock.
* **Decision**: matching (comparison, duplicates, rules, review task) runs in the same transaction that stores the extraction, and after corrections, deletes and rule re-evaluation. It first takes a transaction-scoped advisory lock for the document's department, then row locks in a fixed order (department → document → review task). The trigger document and every related document (same order reference, number or amount, duplicate links, previous comparison partners) are re-evaluated; documents still processing are skipped (their own run matches them).
* **Consequences**: + the final state does not depend on arrival order (tested: the order arriving after the invoice clears its warning), no deadlocks between workers and API requests. − matching serializes per department; one run touches a handful of documents, so this costs milliseconds per document, but a department with very high upload rates would need a queue-based matcher.

### ADR-035 — One review task per document; findings are acknowledged by stable keys
* **Context**: ADR-025 put review reasons on the document; Module 26 needs assignment, priorities, SLAs and a decision record, and re-running matching must not resurrect findings a person already accepted.
* **Decision**: every finding (processing reason, failed or unverifiable rule, duplicate) becomes a reason with a stable key (`reason:CODE`, `rule:CODE:OUTCOME:<items>`). A document has at most one open task (partial unique index) and is `REVIEW_REQUIRED` exactly while it is open. Priority comes from the most severe finding (a warning one level lower); due date from `REVIEW_SLA_HOURS`. A human resolution (`APPROVED`, `CORRECTED`, `REJECTED` with a note) acknowledges the task's keys for that document version: they do not reopen a task, a new key does. When all findings disappear, the task closes as `CLEARED`; a new file version cancels the old version's task. `documents.review_reasons` keeps the raw machine findings.
* **Consequences**: + one place to work, no duplicate tasks, decisions survive re-evaluation, and the queue shows why each document is there. − a changed finding (different line, different outcome) is a new key and asks again; a rejection marks the document as decided, it does not delete or block it (workflow actions on rejected documents arrive in Phase 8).

### ADR-036 — Tolerances live in the rules
* **Context**: the comparison needs price, quantity and tax-rate tolerances, and the rules decide pass/fail on the same differences; two settings for one decision drift apart.
* **Decision**: tolerances are rule parameters (`INV_PO_UNIT_PRICE`, `INV_PO_QUANTITY`, `INV_PO_TAX_RATE`, `INV_DELIVERED_QUANTITY`); `tolerances_from_rules` derives the comparison's tolerances from the enabled rules, and every comparison stores the tolerances it used. Only the confidence threshold (`COMPARISON_MIN_CONFIDENCE`) is a setting, because it is about extraction quality, not business policy.
* **Consequences**: + an administrator changes a tolerance in one place, validated and audited; old comparisons still show what they were judged against. − changing a rule does not re-judge processed documents by itself: `POST /rules/evaluate` or `make match` does.

### ADR-037 — A difference that rests on a weak reading is "could not be verified", not a discrepancy
* **Context**: on scans, a misread digit or a lost table row looks exactly like a wrong price or a short delivery; failing the document blames the supplier for an OCR error (the first scanned evaluation runs showed this).
* **Decision**: a comparison item is `UNCERTAIN` when a differing value is a weak machine reading (reading confidence below `COMPARISON_MIN_CONFIDENCE`, or ambiguous, and not corrected by a person), when currencies differ, when no line at all could be read on the other document, or when a line found no twin and its item code, or one left unpaired on the other document, is a weak reading. Rules turn uncertain items into `WARN` with "Could not be verified". The document-arithmetic rule warns instead of failing when a value in the failed sum is weak. The reading confidence is the field confidence without its consistency factor: a failed check means the document disagrees with itself, which must not turn a confirmed error (a wrong printed total) into "could not be verified". Both outcomes send the document to review; only `FAIL` asserts a discrepancy.
* **Consequences**: + reviewers see whether to check the reading or the supplier; on scans FAIL precision rose from 66.7% to 80.0% when misread item codes stopped failing (discrepancy report). − a real discrepancy on a badly scanned document is reported as a warning; it is still in the queue, with the same priority one level lower.

### ADR-038 — An invoice without its purchase order on file warns until the order arrives
* **Context**: invoices often arrive before the order is uploaded, or reference an order in another department; a missing reference is a policy violation, a missing upload is not.
* **Decision**: `INV_MISSING_PO` fails when the invoice carries no order reference, and warns when it references an order that is not on file in the invoice's department. Matching re-runs when the order arrives (ADR-034), which clears the warning and adds the comparison.
* **Consequences**: + no false discrepancy for upload order, and cross-department orders are never looked at. − an order filed in another department leaves the invoice warning until someone moves or re-uploads it.

### ADR-039 — Manual comparisons are stored but run no rules
* **Context**: users want to compare an invoice with an order it does not cite; rules and review tasks are driven by automatic matching, and two comparisons of one invoice must not fight over its review state.
* **Decision**: `POST /comparisons` stores a `MANUAL` comparison of documents the caller can see, with the current tolerances; it creates no rule results and no review task. Automatic matching keeps exactly one `AUTO` comparison per subject (partial unique index), replaced on each run.
* **Consequences**: + exploration never changes a document's review state. − a manual comparison that shows a problem needs a person to act on the document (correct the order reference, then matching takes over).

### ADR-040 — Contract versions are compared clause by clause, aligned by title before number
* **Context**: Module 27 asks for added, removed and modified clauses between versions; inserting a clause renumbers everything after it, so comparing "clause 7" with "clause 7" reports changes that are not there.
* **Decision**: clauses are segmented from the stored page text by numbered headings ("7.", "7.2", "Clause 7", "Article 7"); the text before the first heading is the preamble and the signature block is separate. Clauses are aligned by title similarity, then by number with similar text, then by text alone; pairs are UNCHANGED or MODIFIED with a word-level diff and a renumbered flag; the rest are ADDED or REMOVED. The comparison is computed on request, not stored.
* **Consequences**: + renumbering is not reported as change; works for native and OCR text alike (measured in the versions report). − documents without numbered headings are one clause; a clause renamed and rewritten at once is reported as removed + added.

### ADR-041 — Matching decisions are a pure function, evaluated with the production code
* **Context**: the evaluation must measure what the service decides, not a re-implementation of it.
* **Decision**: `matching.service.assess` takes a document's facts plus candidate orders, delivery notes and possible duplicates and returns the order picked, the comparison, the duplicates and the rule results; the service only loads candidates from the database and stores results. The discrepancy suite builds the same inputs from extracted synthetic documents (all bundles in one department) and calls `assess`.
* **Consequences**: + reported precision and recall are those of the shipped logic; the database pre-selection (by reference, number, amount) is mirrored in a few lines of the suite. − the suite does not exercise locking and persistence; integration tests cover those.

### ADR-042 — Three embedding providers, one vector column, and full text as the fallback
* **Context**: the user's Gemini key is on the free tier (synthetic data only), the build environment has neither a key nor access to model downloads, and confidential passages may not reach an external model at all.
* **Decision**: `EMBEDDING_PROVIDER` selects `gemini` (external, behind the sensitivity gate), `fastembed` (local ONNX model, optional extra) or `hashing` (offline signed feature hashing of words, word pairs and character n-grams — lexical, not semantic). All emit 768-d unit vectors into the same pgvector columns; each chunk stores the model that produced its vector, queries only match vectors of the configured model, and `docintel reembed` fills in what is missing or stale. A chunk without a vector (no provider, blocked by the gate, provider failure after the last retry) is still found by full-text search.
* **Consequences**: + RAG works with no key and no download, and degrades instead of failing; switching models is one command. − only the lexical hashing model could be measured here; the semantic models are "Not yet measured" and the dense similarity threshold must be calibrated per model.

### ADR-043 — Section-aware chunks carry title and breadcrumb into the index
* **Context**: policy questions are answered by one section; a chunk that spans sections cannot be cited precisely, and a chunk without its heading loses the words that identify it ("4.1 Tolerance").
* **Decision**: chunks never cross a section; paragraphs are packed to ~500 tokens (hard 800, sentence then word splits), tables stay whole or split by row groups with the header repeated, consecutive chunks of a section share ~75 tokens of whole sentences. The document title and section breadcrumb are stored separately (`context_prefix`) and are part of the embedded text and of the full-text vector (weight A, content B).
* **Consequences**: + every citation names one section; the evaluation measured the prefix at +0.13 MRR on kb-queries (0.938 vs 0.812) and section chunks above fixed-size windows (0.938 vs 0.841). − small sections make small chunks (mean ~56 tokens on the seed knowledge base); the holdout set (10 questions) is too small to confirm the prefix effect.

### ADR-044 — Versions of a knowledge document are cited by retrieval window
* **Context**: outdated policy must not be cited, but "what applied in June 2025?" is a legitimate question, and a new version uploaded before its effective date must not hide the current one.
* **Decision**: each upload is a row; rows with the same `document_key` are versions, at most one ACTIVE (partial unique index), version changes serialized by an advisory lock on the key. When a version is processed it becomes ACTIVE if it takes effect later than the active one (same or unknown date: the later upload), otherwise it is stored as SUPERSEDED history. Each version's retrieval window (effective_from, or the publication date for an undated replacement; effective_to capped the day before its successor starts) is copied onto its chunks, and retrieval filters `as_of` (default today) inside the index scan. Archiving the active version restores the latest earlier one.
* **Consequences**: + no superseded passage was returned as of the evaluation date, none not yet in force as of a past date (retrieval report); the outcome does not depend on upload or processing order. − every version of a key must keep the same audience (enforced at upload); archiving a middle version leaves a gap in history.

### ADR-045 — Hybrid retrieval: RRF, full-text order chosen per mode, and an IDF coverage gate
* **Context**: questions mix meaning with exact tokens (clause numbers, amounts); `ts_rank_cd` ignores how rare a term is; a vector search always returns a "nearest" passage, even for questions the knowledge base cannot answer.
* **Decision**: dense (pgvector HNSW, cosine, iterative scan) and full-text candidates (OR of the question's stemmed terms) are fused with Reciprocal Rank Fusion (k=60), ties broken deterministically. Full-text candidates are ordered by IDF-weighted term coverage when full text is the only retriever and by `ts_rank_cd` when fused — the better of the two in each mode on kb-queries. The evidence gate requires the best of the top five passages to cover ≥ 25% of the question's IDF-weighted terms, or a dense similarity ≥ 0.5; otherwise the answer is "insufficient evidence" without a model call.
* **Consequences**: + measured: hybrid MRR 0.938 (kb-queries) / 0.950 (holdout), full text alone 0.918 / 0.883; filters in SQL, so no unauthorized passage is ever loaded. − thresholds were chosen on kb-queries; on the holdout the gate refused only 2 of 4 unanswerable questions (no false refusals), so the answer model's own "insufficient evidence" and the citation checks remain necessary.

### ADR-046 — Answers are composed from verified claims, never from free text
* **Context**: a fluent answer can contain statements no source supports; citations to sources that were never provided are a classic hallucination.
* **Decision**: the model returns only claims, each with the labels of the sources it relies on (structured output). Citations must name a provided source; claims without one are dropped. Each claim is checked against its cited text: every number must occur there and ≥ 60% of its content words; failing claims are kept but flagged. The answer shown is the surviving claims with their citations; statuses are ANSWERED, PARTIALLY_SUPPORTED, INSUFFICIENT_EVIDENCE or RETRIEVAL_ONLY. Sources are wrapped in per-request random markers and declared untrusted data.
* **Consequences**: + every sentence the user reads carries a checked citation; a source cannot close the delimiter block. − the lexical grounding check cannot detect a claim that reuses the source's words with a different meaning; generated-answer quality is "Not yet measured" (needs an LLM key).

### ADR-047 — The sensitivity gate applies per source, and to the question itself
* **Context**: a Legal user may ask about a CONFIDENTIAL playbook while the external model may only see INTERNAL content; refusing the question would hide information the user is allowed to read.
* **Decision**: sources above `AI_EXTERNAL_MAX_SENSITIVITY` (label or detected content) are not sent to an external model but are returned to the user as passages, with a notice and `sent_to_model=false`; with no allowed source the answer is RETRIEVAL_ONLY. Chunks store the effective sensitivity of their document. The question is embedded externally only if it contains no restricted data (card numbers, SSNs).
* **Consequences**: + the user still sees every passage they may read; nothing above the limit leaves the deployment (tested). − an answer may be incomplete when a key source was withheld; the notice says so.

### ADR-048 — Business document search is parsed deterministically
* **Context**: "invoices from Vendor X", "contracts containing termination clauses", "payment terms longer than 60 days" are filters on extracted data plus free text; a model-based query planner would send every query outside the platform and be hard to test.
* **Decision**: a rule-based parser recognizes document types, vendor phrases (resolved against the vendor master by key, alias or name; printed names otherwise), payment-term and total comparisons, months, years and ISO date ranges; the rest is free text for hybrid search over `document_chunks`. Filters run in SQL on the current extraction (corrections win); documents whose payment terms were not extracted are matched from terms written in their text ("within 75 days"), and say so. The interpretation is returned with the results.
* **Consequences**: + no external call, explainable, measured at 100% precision and recall on the structured question families of the search report. − phrasing outside the recognized patterns becomes free text; an LLM query planner can be added behind the same interface if users need it.

### ADR-049 — The investigation agent is a bounded LangGraph graph whose model never picks tools
* **Context**: Module 14 asks for an explicit-state workflow; free-form tool-calling agents let the model (and anyone who can put text in front of it — a request, a document, a policy) decide which tools run, which makes runs hard to bound, test and secure.
* **Decision**: a LangGraph `StateGraph` with typed, JSON-compatible state and fixed nodes (understand → identify → inspect → rules → [compare] → knowledge → analyse → confidence → recommend → approval gate → act/propose → finish). The model can only fill a typed plan (intent enum, bounded search text, document numbers, policy questions, field names) and write findings; conditional edges and node code decide which tools run. One follow-up retrieval round at most; tool, model-call and time budgets; runs in the worker, one attempt.
* **Consequences**: + every path through the graph is enumerable and tested; a hijacked model cannot call anything new (security tests). − the graph only does what its nodes anticipate; new kinds of investigation need new nodes, not a new prompt.

### ADR-050 — Facts come from tools; a model may only add checked findings
* **Context**: an investigation mixes facts (extracted values, rule outcomes), policy text and interpretation; a model that writes all of it can state a passing check that failed or a number nobody read.
* **Decision**: deterministic findings (observed facts, rule results, uncertainties, policy references) are written from tool outputs with evidence labels. A model may add RETRIEVED_KNOWLEDGE (grounding-checked against the cited passage) and AI_INFERENCE findings; they must cite labels that exist, every number must occur in the cited evidence, and statements that clear a failed rule (cited, issue-level or blanket) are removed. The same checks apply to the summary and the rationale. Without a model, the deterministic summary and recommendation stand.
* **Consequences**: + the facts of a run never depend on a model; the scripted adversary in the evaluation kept 0 statements and 0 summaries. − the clearance checks are lexical patterns: a model can phrase a misleading inference they do not catch; it is labelled "AI inference" and cannot change the recommendation's guardrails.

### ADR-051 — Allowlisted recommendations, guardrails and a risk table; only a review request executes
* **Context**: "recommend action → human approval" (Modules 14, 17); the approval workflow arrives in Phase 8, but the agent must already never act beyond what is safe.
* **Decision**: five actions (APPROVE_FOR_PAYMENT, REJECT_DUPLICATE, REQUEST_VENDOR_CLARIFICATION, HOLD_FOR_REVIEW, NO_ACTION) with a risk table. A model's proposal stands only if the guardrails allow it (e.g. payment needs an invoice with no failed or unconfirmed rule, no difference, no duplicate and HIGH confidence); otherwise the rules decide. HOLD_FOR_REVIEW may execute (a review request, `allow_safe_actions`); every other action is recorded as PROPOSED with the role that must approve it.
* **Consequences**: + 0 unsafe recommendations in the evaluation, including against an adversary that always proposes payment. − until Phase 8, a proposed approval is information for a person, not a workflow item.

### ADR-052 — Confidence is computed from the weakest input of each kind
* **Context**: an LLM's self-reported confidence is not calibrated; summing penalties per document made multi-document investigations look worse than any of their documents.
* **Decision**: start at 1.0 and subtract the worst penalty of each kind (identification, extraction review level, unverified key evidence, rules that could not decide, rules that could not confirm, uncertain comparison items, stale stored outcomes, missing policy, rejected model statements); HIGH ≥ 0.8, MEDIUM ≥ 0.55. Each factor is shown with its reason.
* **Consequences**: + explainable and reproducible; guardrails can use it (payment needs HIGH). − the weights are judgement, not calibrated on real outcomes (Phase 10 calibration).

### ADR-053 — Review requests are review items, so a requested review survives re-evaluation
* **Context**: the agent's `create_review_task` must add something a reviewer sees, but review tasks are rebuilt from findings on every matching run, which would silently drop an added reason.
* **Decision**: a `review_requests` row (document version, requester, run, priority, reason) is loaded by `sync_review` with the rule and processing items; it keeps the version's task open (type REQUESTED_REVIEW when alone) until a person resolves the task, after which it no longer counts. The same request twice is idempotent. Also exposed in REST (`POST /review-tasks/requests`).
* **Consequences**: + requests behave exactly like findings (priorities, SLA, acknowledgement); tested through re-evaluation and resolution. − a request is tied to a version: a new version starts without it.

### ADR-054 — run_business_rules is a dry run; identification keeps exact document numbers only
* **Context**: an investigation must see current rule outcomes without rewriting matching state (which would replace comparisons other runs cite), and "invoice INV-7 from Kestrel" must not drag in Kestrel's other invoices (the first evaluation run showed it did: 18% exact identification).
* **Decision**: `MatchingService.dry_run` loads the same candidates and calls the same `assess` as the worker, stores nothing and reports stored outcomes next to live ones; comparisons and duplicates involving documents the caller cannot see are hidden. Search hits carry the printed number and order reference; when a request names document numbers, only exact matches (own number, else the quoted order number) are investigated, and "none found" is said rather than guessed.
* **Consequences**: + 100% exact identification on both evaluation datasets; investigations never change matching state. − a misread document number on a scan makes the document unfindable by number (it is still found by vendor and month).

### ADR-055 — MCP serves the same registry with personal, scoped tokens re-checked on every call
* **Context**: Module 16: MCP only where it adds value. The value is using the verified tools from an assistant without exporting documents; the risk is a second, weaker door into the platform.
* **Decision**: a thin adapter (official SDK, low-level server) over the agent's tool registry: same schemas, permissions, access predicates, logging. Identity is a personal API token (SHA-256 stored, shown once, scoped to tool permissions and the owner's role, expiring, revocable), re-validated on every call, so revocation, deactivation and demotion apply inside open sessions. stdio (one local user) and streamable HTTP (bearer middleware, DNS-rebinding protection). No investigation, approval or administration tools.
* **Consequences**: + nothing an MCP client does bypasses what the web UI enforces; calls are audited per call. − one database round trip per call for authentication; tokens are bearer secrets the user must keep (shown once, revocable).
