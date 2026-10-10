# Project overview

A summary of what was built, how and why, for readers who will not read the whole
repository: reviewers, interviewers, new contributors. Every statement refers to code in this
repository; every number comes from a committed report (`evaluation/reports/`,
`evaluation/load/`) and is measured on synthetic data.

## Title

**Enterprise Document Intelligence Platform** — document understanding, verification,
cited retrieval, a tool-using agent and human-approved workflows for finance and procurement
documents, as one deployable modular monolith (FastAPI, PostgreSQL + pgvector, React).

## Resume bullets

* Built a document-intelligence platform (FastAPI, PostgreSQL/pgvector, React, Docker) that
  turns invoices, purchase orders, delivery notes and contracts into schema-validated fields with
  page-level evidence, checks them with 19 deterministic business rules and routes exceptions to
  a review queue; 100% field F1 on native PDFs and 0.996 on 150-DPI scans of the synthetic
  benchmark, with an auto-accept threshold calibrated on one dataset and confirmed on a held-out
  one (0 errors among 34 auto-accepted documents).
* Designed a LangGraph investigation agent that may only *propose* actions through 10
  permission-checked tools, and invoice/contract workflows with maker-checker approval; measured
  0 unsafe recommendations against a scripted adversarial model and 0 bypasses in 152 attempts by
  makers or too-junior roles, with every state change audited in an append-only log.
* Implemented hybrid retrieval (pgvector HNSW + PostgreSQL full text, reciprocal rank fusion)
  with access and version filters inside both scans and an evidence gate before generation;
  hit@5 100% and MRR 0.938 on the tuning set (0.950 on the holdout) with offline embeddings.
* Built the evaluation harness (11 suites, 56 regression gates in CI, README numbers generated
  from the reports) and productionized the stack: Prometheus metrics, alert rules with unit
  tests, per-caller rate limits shared across replicas, hardened containers, a release pipeline
  with image scanning and a deploy script with automatic rollback (rehearsed, not yet deployed).
* Load-tested the deployed shape through nginx and found that PostgreSQL spent three times
  longer planning than executing the hottest queries; restructuring their eager loading raised
  single-process throughput from 47 to 58 reads/s and cut p95 latency from 284 to 207 ms, meeting
  the 300 ms target at 8 concurrent users while documents were being processed.

Phrase the bullets for the role: the agent and RAG bullets for AI-engineering roles, the
evaluation and load-test bullets for ML-platform or backend roles.

## Architecture

One backend package (`docintel`) with three entry points over the same code: the HTTP API, the
background worker and the command line (plus an MCP server). PostgreSQL is the only stateful
service: relational data, vectors (pgvector), full-text search, the job queue (`FOR UPDATE SKIP
LOCKED` with leases and `LISTEN/NOTIFY`), rate-limit counters and the append-only audit log.
Files go to a storage interface (local filesystem or any S3-compatible bucket); models go
through provider interfaces (Gemini, a self-hosted Ollama model, or local fallbacks), so the
platform runs fully offline in deterministic mode.

```
React SPA ─> nginx ─> API replicas ─┐
                                   ├─> PostgreSQL + pgvector (data, vectors, FTS, queue, audit)
             worker(s) ────────────┘      storage (local / S3)      model providers
```

A document upload is validated (content sniffing, page and pixel limits, password-protected
files refused), stored under a server-generated key, and queued in the same transaction as its
rows. The worker inspects every page (native text or OCR), runs Tesseract where needed, builds
layout blocks and tables, classifies, extracts fields with evidence, matches the document with
its order and delivery notes, applies the rules and opens a review task when something needs a
person. Layering is enforced by import-linter contracts in CI. Decisions are recorded as 80
ADRs (`docs/architecture/13-decision-log.md`).

## Technical challenges worth discussing

* **Trusting nothing the model says about itself.** Field confidence is computed from measured
  signals (evidence found on the page, normalization, OCR confidence, agreement between the
  layout reader and the model, arithmetic consistency), never from the model's self-assessment.
  The auto-accept threshold was then chosen by a rule fixed in advance on a development set and
  reported unchanged on a held-out set, with a Clopper-Pearson bound on the error rate.
* **Layout first, model second.** A geometry-based extractor reads every document; the model is a
  second reader whose values must be found on the page and agree with the first. This made the
  platform useful with no model at all, and made prompt injection in documents unable to reach
  an auto-accepted value.
* **A forged client address.** While adding a per-address login limit, the audit log turned out
  to record whatever `X-Forwarded-For` a client sent: nginx appended to the header and uvicorn
  trusted its first entry. Fixed at the proxy (overwrite, trust only the TLS proxy) and pinned by
  a smoke check.
* **Planning cost under load.** The load test showed the database re-planning parameterised
  queries on every execution, and the review queue joining eleven tables through eager loads;
  moving people to batched loads cut its planning from 9.1 to 1.7 ms. Forcing generic plans was
  rejected because the job queue depends on partial indexes a generic plan cannot use.
* **Sessions that survive reloads without looking like token theft.** Refresh-token rotation
  with reuse detection revoked sessions when a page reload aborted a refresh response; a short
  grace window for an unused successor fixed it, found by the end-to-end browser test.
* **pdfium is not thread-safe.** Every PDF call goes through one process-wide lock; PDF work
  scales by worker processes, which the queue supports.

## Machine learning

* **OCR**: Tesseract 5 with preprocessing chosen by ablation — upscaling of low-DPI images
  (100 DPI without it: 7.1% CER, with it: 1.8%) and projection-profile deskew; character error
  1.2% on clean pages, 10.4% on heavy 150-DPI scans.
* **Classification**: a calibrated TF-IDF + logistic-regression model trained at worker start
  from a synthetic corpus plus human corrections, with a model fallback when it is unsure.
  Accuracy 100% on rendered documents; 99.7% from the first 300 characters only.
* **Extraction**: versioned schemas per document type; the layout extractor for every document;
  Gemini or a local model as a second reader, with images for low-confidence pages; values
  verified against the page, normalized (dates, amounts, currencies, vendors against a vendor
  master) and scored; documents routed to auto-accept, analyst review or mandatory review.
* **Honest limits**: synthetic layouts are regular and the classifier's training and test
  generators are related, so these numbers overstate real-world accuracy; model-assisted
  extraction and classification are not yet measured (no model in the build environment).

## Agent

A LangGraph `StateGraph` with explicit typed state: understand the request, identify the
documents, gather facts with controlled tools (search, fields, evidence, comparison, rules,
knowledge base), analyse, score confidence, recommend, and pass guardrails that remove blanket or contradicting model statements and refuse a
payment recommendation for an invoice whose rules failed. Tools are permission-checked, schema-validated, time- and size-bounded and logged;
the same registry is served to MCP clients with scoped, hashed, revocable API tokens. The agent
cannot execute high-impact actions: workflows turn its proposals into actions that a different
person with the right role must approve (maker-checker enforced in the service and by a
database CHECK). In deterministic mode (no model) the graph plans and analyses with rules: 100%
task success on 70 development and 60 held-out runs, 0 unsafe recommendations.

## Retrieval-augmented generation

Knowledge documents are chunked along their structure (headings, sections, breadcrumbs as a
prefix), embedded (Gemini, local fastembed, or an offline hashing model) and indexed for full
text. A question runs both scans with the caller's department, the document version window and
category filters inside each, fuses them with reciprocal rank fusion and passes an evidence gate
(term coverage or similarity) before any model call: without adequate evidence the answer is
"insufficient evidence". Sources are wrapped in nonce-delimited markers and declared untrusted;
the answer shown is composed only from claims whose citations name provided sources and match
them. Ablations: full text alone MRR 0.918, dense alone 0.885, without the title prefix 0.812,
fixed-size chunks 0.841, hybrid 0.938. Generated-answer quality is not yet measured (needs an
LLM).

## Security

Argon2id passwords, JWT with strict claims, lockout, roles re-read on every request, a
code-defined permission matrix, department scoping as SQL predicates (outside documents are
404s); httpOnly `SameSite=Strict` refresh cookies with rotation and reuse detection; upload
validation by content; an append-only audit log enforced by a database trigger; a sensitivity
gate that keeps confidential content away from external models; per-caller rate limits;
token-protected metrics; hardened read-only containers; secrets only in a git-ignored `.env`;
gitleaks, `pip-audit` and `npm audit` in CI. The security test plan
(`docs/architecture/09-security-architecture.md`) maps each threat to a test.

## Evaluation methodology

Seeded synthetic generators produce documents with recorded ground truth and planted defects
(price, quantity, tax, vendor, missing order, duplicates, multi-page, scanned). Eleven suites
measure OCR, classification, tables, extraction and calibration, discrepancies, contract
versions, retrieval, document search, the agent, workflows and system performance; each writes
a JSON and Markdown report with its dataset, seeds, configuration and environment. Development
and held-out datasets are separated where a suite drove changes. 56 regression gates hold
safety invariants exactly and accuracy a little below the measured value; CI runs every suite
on quick datasets. The README's table is generated from the reports, and anything not measured
says "Not yet measured" with the reason.

## Deployment

Staging and production run the same compose stack on one Docker host: registry images by
commit, Caddy for TLS, API and worker replicas, nightly database dumps and a dump before every
migration, daily retention. A release workflow builds, scans and pushes the images; a deploy
workflow runs a script on the host that migrates, waits for health checks and starts the
previous release again if the new one does not become ready. The layout was rehearsed on a local
Docker host, including a deliberately broken release that was rolled back. **No staging or
production deployment has been performed**, and the GitHub workflows have not run yet.

## Interview questions

**Why one PostgreSQL instead of a vector database, Redis and a queue?** One transactional
store keeps the upload, its job and its audit event atomic, makes access filters part of the
vector query, and leaves one service to back up. pgvector's HNSW with iterative scans handles
filtered search at this scale; the queue needs `SKIP LOCKED` and `LISTEN/NOTIFY`, not a broker.
The cost is that very large vector collections would need re-evaluation.

**How do you keep an LLM from making a wrong value look certain?** It never scores itself.
A value must be found on the page, survive normalization and agree with the layout reader;
confidence combines those signals, and the auto-accept threshold was calibrated and checked
on held-out data.

**What stops the agent from approving a payment?** Structure, not instructions: it has no tool
that executes anything, its recommendations pass a guardrail that checks them against rule
results, and workflow actions need approval by a different person with the right role, enforced
in the service and by a database constraint.

**How do you evaluate retrieval without real data?** Question sets with labelled relevant
passages over the seeded knowledge base, a tuning set and a holdout, hit@k, MRR and nDCG,
ablations of each component, refusal of unanswerable questions, and a leakage check across
departments.

**What did the load test change?** It showed planning dominated database time for two listing
queries and that reads are CPU-bound in the API process (about 22 ms per read). Batched loading
of related people raised throughput by about a quarter; scaling is by API processes, which the
deployment runs as replicas behind nginx.

**What would you do next with real documents?** Recalibrate the confidence thresholds on
labelled real documents, measure model-assisted extraction and generated answers, turn human
corrections into a growing evaluation set, and deploy to staging with a real load generator.

**What is not production-grade yet?** No deployment has been performed; one host is one failure
domain; migrations and the application share a database role; alerts have no delivery channel
configured; model-assisted paths are unmeasured.
