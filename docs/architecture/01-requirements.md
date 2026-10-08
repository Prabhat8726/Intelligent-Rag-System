# 01 — Product Requirements & Requirements Analysis

Status: **Phase 0 baseline** · Owner: lead engineer · Last reviewed: 2026-10-08

This document restates the master prompt as testable requirements, records every
contradiction / gap / weak choice found during analysis, and states how each one
is resolved. Later documents reference requirement IDs (`FR-*`, `NFR-*`).

---

## 1. Product statement

An enterprise platform where a company uploads business documents (invoices,
purchase orders, receipts, delivery notes, contracts, policies, resumes, bank
statements, forms) and the system:

1. understands them (OCR / layout / tables / classification),
2. extracts schema-validated fields **with provenance**,
3. normalizes, compares and verifies them with **deterministic rules first**,
4. grounds AI reasoning in retrieved company policy (**RAG with citations**),
5. lets an **agent** investigate discrepancies using controlled tools,
6. routes decisions through **human approval**, and
7. records an **immutable audit trail**.

It is explicitly *not* an OCR demo, chatbot, or LLM wrapper: the LLM is one
component behind interfaces, and deterministic facts always take precedence.

## 2. Personas → system roles

The prompt lists five personas and five RBAC roles without mapping them. Mapping:

| Persona | Default role | Department |
|---|---|---|
| Operations Analyst | `ANALYST` | Operations |
| Finance Analyst | `ANALYST` | Finance |
| Procurement Analyst | `ANALYST` | Procurement |
| Legal / Compliance User | `REVIEWER` (contracts/policies) | Legal |
| Team lead approving payments/holds | `MANAGER` | any |
| Auditor / read-only stakeholder | `VIEWER` | any |
| Administrator | `ADMIN` | — |

Persona ≠ role: personas differ by **department** (data scope), roles differ by
**capability**. Authorization = role permissions ∩ department scope (see
[09-security-architecture.md](09-security-architecture.md)).

## 3. Functional requirements

| ID | Requirement | Module(s) | Phase |
|---|---|---|---|
| FR-01 | Upload PDF, PNG, JPEG, TIFF; reject everything else (DOCX/XLSX later) | M1 | 2 |
| FR-02 | Track id, filename, type, MIME, size, page count, uploader, status, SHA-256, version, source, processing duration, extraction status | M1 | 2 |
| FR-03 | Document states `PENDING → PROCESSING → COMPLETED / FAILED / REVIEW_REQUIRED` | M1 | 2 |
| FR-04 | `DocumentStorage` abstraction: local FS (dev) and S3-compatible object storage (prod) | M2 | 2 |
| FR-05 | Distinguish native PDF / scanned PDF / image per **page**; OCR only where needed; keep page numbers and bounding boxes | M3 | 3 |
| FR-06 | Classify into 9 types (`INVOICE … OTHER`) with *measured* confidence; human correction stored | M4 | 3 |
| FR-07 | Layout: paragraphs, headings, tables (incl. multi-page), key-value pairs, lists, headers/footers, signatures where feasible | M5 | 3 |
| FR-08 | Schema-based extraction per document type; validate model output; survive malformed JSON | M6 | 4 |
| FR-09 | Provenance per field: value, page, source_text, bbox (if available), confidence | M7 | 4 |
| FR-10 | Normalize dates, currencies, amounts, vendor names; keep original and normalized | M8 | 4 |
| FR-11 | Compare Invoice↔PO, PO↔Delivery, Contract↔Policy, Resume↔JD, Contract v1↔v2 with evidence from both sides | M9, M27 | 5 |
| FR-12 | Deterministic, configurable business rules (duplicates, qty/price/tax/vendor mismatch, missing PO, expired contract, policy violation, mandatory fields) | M10 | 5 |
| FR-13 | AI reasoning that labels every statement as `OBSERVED_FACT`, `RULE_RESULT`, `RETRIEVED_KNOWLEDGE`, `AI_INFERENCE` or `UNCERTAINTY`; AI cannot override deterministic facts | M11 | 7 |
| FR-14 | Knowledge base of policies / procedures / guidelines / FAQs with chunking, metadata, embeddings in pgvector | M12 | 6 |
| FR-15 | RAG with filtering, hybrid retrieval, context assembly, citations, retrieval evaluation, hallucination safeguards | M13 | 6 |
| FR-16 | Agent workflow with explicit state (LangGraph) over 10 controlled tools | M14, M15 | 7 |
| FR-17 | MCP server exposing a subset of tools, same authz | M16 | 7 |
| FR-18 | HITL states `PROPOSED → AWAITING_APPROVAL → APPROVED/REJECTED → EXECUTED/FAILED` storing user, time, action, transitions, reason | M17 | 8 |
| FR-19 | Workflows: Invoice Processing, Contract Review | M18 | 8 |
| FR-20 | Dashboard, inbox, document detail, comparison view, AI analysis, approvals, audit, search | M19 | 9 |
| FR-21 | Auth + RBAC (`ADMIN, MANAGER, ANALYST, REVIEWER, VIEWER`) enforced server-side | M20 | 0 |
| FR-22 | Background processing `QUEUED → PROCESSING → COMPLETED/FAILED` | M23 | 2 |
| FR-23 | Confidence routing: high → auto, medium → analyst review, low → mandatory review | M26 | 4–5 |
| FR-24 | Semantic search + metadata filters over business documents | M28 | 6 |
| FR-25 | Duplicate detection: hash, metadata, invoice no., vendor, date, amount, semantic similarity | M29 | 5 |
| FR-26 | Reports: invoice verification, comparison, compliance, AI analysis (sources, findings, evidence, rules, recommendation, human decision, timestamp) | M30 | 8 |
| FR-27 | Synthetic document generator with controlled defects + ground truth | M34 | 2 (+5) |
| FR-28 | Evaluation harness producing metrics **only from real runs** | M25 | 10 |
| FR-29 | Reproducible end-to-end demo (17 steps, §50 of the prompt) via `make` targets | §50–51 | 10 |

## 4. Non-functional requirements

| ID | Requirement | Target / verification |
|---|---|---|
| NFR-01 Security | OWASP ASVS L2-inspired controls; uploads and retrieved text treated as untrusted | security test suite (Phase 0 → 11) |
| NFR-02 Privacy | Sensitive docs never sent to an external AI provider unless policy allows it | provider routing gate (Phase 3) + tests |
| NFR-03 Auditability | Every security-relevant action produces an append-only audit record | DB trigger blocks UPDATE/DELETE (Phase 0) |
| NFR-04 Replaceability | LLM, embeddings, OCR, vision, vector store, storage behind interfaces | provider registry + contract tests |
| NFR-05 Honesty | No fabricated metrics; README numbers generated by `make evaluate` | "Not yet measured" until measured |
| NFR-06 Cost | Runs on free tiers; LLM calls minimized, rate-limited, cached, budgeted | usage table + limiter (Phase 0/4) |
| NFR-07 Operability | Health/readiness probes, structured JSON logs, Prometheus metrics | Phase 0 (health/logs), Phase 11 (metrics) |
| NFR-08 Reliability | Idempotent processing stages; retries with backoff; job leases | Phase 2 |
| NFR-09 Performance | Not yet measured. Targets are set after the first baseline in Phase 10 | Phase 10 |
| NFR-10 Maintainability | Typed Python (mypy strict on new code), ruff, tests in CI, migrations only | CI (Phase 0) |

## 5. Contradictions, gaps and weak choices — and resolutions

| # | Finding | Why it matters | Resolution |
|---|---|---|---|
| C1 | **"Sensitive business documents" vs "free API tiers".** Gemini API *unpaid* terms state that prompts/responses may be used to improve Google products, may be read by human reviewers, and explicitly say *"Do not submit sensitive, confidential, or personal information to the Unpaid Services."* | Using the free tier with real company documents would violate the provider's terms and the platform's own security principles. | Every document carries a `sensitivity` label (`PUBLIC / INTERNAL / CONFIDENTIAL / RESTRICTED`). A routing gate (`AI_EXTERNAL_MAX_SENSITIVITY`, Phase 3) blocks external AI calls above the allowed level and falls back to local OCR/extraction or manual review. Development/demo uses **synthetic documents only**. Real data requires a paid Gemini tier or a local model (Ollama) — documented, not hidden. |
| C2 | "Provide confidence" vs "do not treat LLM self-confidence as ground truth". | LLM self-reported confidence is uncalibrated. | Confidence is computed from measurable signals (OCR word confidence, evidence string match, schema/type validity, cross-field arithmetic, method agreement, classifier probability). Thresholds are configurable and calibrated in Phase 10 against labelled data. See [06-ai-architecture.md §6](06-ai-architecture.md). |
| C3 | Two status vocabularies: document (`PENDING, PROCESSING, COMPLETED, FAILED, REVIEW_REQUIRED`) and job (`QUEUED, PROCESSING, COMPLETED, FAILED, REVIEW_REQUIRED`). | `REVIEW_REQUIRED` is a business outcome, not a job outcome; conflating them breaks retries. | `documents.status` keeps all five states. `processing_jobs.status` = `QUEUED, PROCESSING, COMPLETED, FAILED`. A job that *completes* with low confidence sets the **document** to `REVIEW_REQUIRED`. |
| C4 | HITL states listed, transitions not defined; no separation of duties. | An analyst could approve their own proposal. | Explicit transition table enforced in code + DB; **maker-checker**: the proposer/initiator cannot approve; approval requires the action's `required_role`. |
| C5 | Personas vs roles not mapped; "cross-user access" tests imply a data-scope model that isn't defined. | RBAC alone cannot express "Finance cannot see Legal's contracts". | Single-tenant deployment with **department scoping** layered on RBAC (§2). One policy module is used by REST, agent tools, MCP, search and RAG (query-time filters, never post-filtering). |
| C6 | Vision pipeline (§2 of prompt) runs RAG for every document. | Retrieval is only useful when reasoning needs policy context; running it per upload wastes quota. | RAG is invoked by the agent / workflow steps that need policy evidence, not by ingestion. |
| C7 | "Use LangGraph" + "human approval" — LangGraph's `interrupt()` would pause graphs for days. | Paused checkpoints break across deployments (graph code changes) and an approval must be a first-class auditable record anyway. | The graph **ends** in `AWAITING_APPROVAL` after persisting a `workflow_action`. Approval runs a deterministic, allowlisted executor. The agent can only *propose* high-impact actions. See [08-agent-architecture.md](08-agent-architecture.md). |
| C8 | Recommended repo has separate top-level `backend/`, `workers/`, `ai/`, `mcp/`, `migrations/`. | These share one domain model and DB transactions. Separate Python packages would duplicate models or create circular dependencies; a root-level `migrations/` breaks the self-contained backend image. | **Modular monolith**: one Python package `docintel` with sub-packages `api`, `workers`, `ai`, `mcp_server`, …; migrations ship inside the package (`docintel/db/migrations`) so every install can verify its schema head. Three entrypoints (API, worker, MCP) from one image. Strong technical reason recorded in ADR-001. |
| C9 | DB table list includes `roles` and `system_metrics`. | Roles are a closed set tied to code-reviewed permissions; a mutable `roles` table invites privilege escalation via data edits. `system_metrics` duplicates Prometheus and source tables. | Role is a constrained column; role→permission map in code. Dashboard trends computed from source tables; live metrics via Prometheus. Both tables omitted unless a real need appears (ADR-006). |
| C10 | "Use Redis/queue only if genuinely needed." | Redis would be an extra stateful service. | PostgreSQL job queue with `FOR UPDATE SKIP LOCKED` + leases. Enqueue happens in the **same transaction** as the document insert (no lost jobs). Redis not used (ADR-002). |
| C11 | `GET /metrics` listed as a public API endpoint. | Prometheus metrics leak operational details. | `/metrics` served in Prometheus format, protected by a bearer token (`METRICS_TOKEN`) and intended for an internal network. Business KPIs come from `GET /api/v1/dashboard/summary`. |
| C12 | Endpoints listed without versioning. | Breaking changes would break the SPA/MCP clients. | All business endpoints under `/api/v1`. `/health*` and `/metrics` stay unversioned for infrastructure probes. |
| C13 | Embedding provider must be swappable, but pgvector columns have a fixed dimension. | Swapping providers silently mixes incomparable vectors. | Standardize on **768 dimensions** (Gemini `gemini-embedding-001` with `output_dimensionality=768`, L2-normalized; local `bge-base-en-v1.5` is also 768-d). Every chunk stores `embedding_model`; retrieval filters on the active model; switching models requires `make reindex`. |
| C14 | Gemini model names churn quickly (2.0 shut down June 2026; 2.5 retiring on some endpoints Oct 2026; 3.6/3.7/3.8 Flash in 2026). | Hard-coded model IDs rot and make evaluations irreproducible. | Model IDs are configuration, never constants. Defaults pin explicit stable versions (not `*-latest` aliases) so evaluation runs are reproducible; every LLM call records the model ID. `make check-ai` lists the models the user's key can access. |
| C15 | Gemini 3.x guidance deprecates `temperature/top_p/top_k` and replaces `thinking_budget` with `thinking_level`. | Sending deprecated params may error or be ignored. | Provider sends sampling params only when explicitly configured; thinking level is an optional setting. |
| C16 | PyMuPDF is the popular PDF library but is **AGPL-3.0**. | AGPL obligations are a real problem for an enterprise product. | Use `pypdfium2` (Apache-2.0/BSD) for rendering and `pdfplumber` (MIT) for native text/word boxes/tables. |
| C17 | Classification "with measured confidence" but nothing to measure against initially. | Zero-shot LLM labels have no calibrated probability. | Hybrid classifier: local calibrated model (TF-IDF + logistic regression) trained on the synthetic corpus + human corrections, LLM fallback when local confidence is low; calibration (ECE) reported in Phase 10. |
| C18 | Missing: retention/deletion semantics, user management, token revocation, rate limiting, data-at-rest guidance. | Required for an "enterprise" claim. | Soft delete + purge job (Phase 2), admin user management API (Phase 8), refresh-token rotation + revocation (Phase 9), rate limiting (Phase 11), encryption-at-rest via storage/DB provider settings (Phase 11 deployment doc). |
| C19 | "Agent success" / "recommendation correctness" have no ground truth. | Unmeasurable claims. | Synthetic scenarios carry expected outcomes (expected discrepancy codes, expected recommendation, expected tools). See [10-evaluation-plan.md](10-evaluation-plan.md). |
| C20 | Synthetic-only evaluation can overstate real-world accuracy. | Misleading portfolio metrics. | Reports always state the dataset; optional public datasets (e.g. SROIE/CORD receipts, FUNSD forms) are evaluated separately after licence review. |

## 6. Assumptions (made, not asked — per §46 of the prompt)

* **A1** Single organization per deployment (single-tenant); multi-tenancy is out of scope.
* **A2** English-language documents first; normalization handles common US/EU number and date formats; ambiguous dates are flagged `UNCERTAIN` rather than guessed.
* **A3** Gemini API is the default LLM/vision/embedding provider (user-provided key); the free tier is used only with synthetic data (C1).
* **A4** Max upload size 25 MB, max 200 pages per document (configurable).
* **A5** Currency conversion is **not** performed (cross-currency comparisons are reported as `UNCERTAIN`), because exchange-rate sources and dates are business policy.
* **A6** MIT licence for the repository (change if your organization requires otherwise).
* **A7** Desktop-first enterprise UI; responsive but not mobile-optimized.

## 7. Out of scope (explicit)

E-mail ingestion, ERP integration (actions are internal state changes + reports),
e-signature, payments execution, multi-tenancy, model fine-tuning.
