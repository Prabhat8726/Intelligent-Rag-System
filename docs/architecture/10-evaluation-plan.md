# 10 — Evaluation Strategy & Metrics (Module 25)

Rule: **every number in the README or a report is produced by `make evaluate`
(or a named script) from a recorded run.** Until then the value is
"Not yet measured". Reports state dataset name/version, model IDs, prompt
versions and git SHA.

## 1. Datasets

| Dataset | Source | Ground truth | Phase |
|---|---|---|---|
| `synthetic-core` | `docintel.synthetic` generator (fixed seed) | JSON sidecar per document: type, every field, line items, page of each field, injected defects | 2/5 |
| `synthetic-noisy` | Same documents rendered → image degradations: blur, rotation ±3°, JPEG artefacts, salt-and-pepper, low DPI | Same as above + exact page text for CER/WER | 3 |
| `synthetic-scenarios` | PO/invoice/delivery/contract/policy *bundles* with controlled discrepancies (price, quantity, tax, vendor, missing PO, duplicate, expired contract, injection payloads) | Expected discrepancy codes, expected recommendation, expected tool set | 5/7 |
| `synthetic-contracts` | Contract families of three versions with recorded edits (`docintel.synthetic.contracts`) | Clauses added / removed / modified per step; per version the required clauses left out, the termination notice period and the governing law (Phase 8) | 5/8 |
| `kb-queries` | 54 hand-written questions over the seed knowledge base (`knowledge_base/`), 6 unanswerable; used to choose the evidence-gate thresholds and the full-text order (the `dev` split) | Relevant (document_key, section heading) pairs; empty = unanswerable | 6 |
| `kb-queries-holdout` | 14 questions written after those choices, 4 unanswerable; never used for tuning (the `test` split) | Same | 6 |
| Public (optional) | e.g. SROIE / CORD receipts, FUNSD forms | Dataset labels | 10, after licence review; reported separately |

Splits: `dev` (prompt/threshold tuning) and `test` (reported numbers only).
Synthetic data overstates real-world accuracy; reports say so explicitly (C20).

## 2. Metrics

| Area | Metrics | Notes |
|---|---|---|
| Classification | accuracy, macro precision/recall/F1, per-class F1, confusion matrix, **ECE** (calibration) | Local model vs LLM fallback vs ensemble reported separately |
| OCR | CER, WER vs generator text | Per degradation type |
| Extraction | field-level exact match, normalized match (dates/amounts/vendors after normalization), per-field precision/recall/F1 (null handling explicit), line-item F1 via optimal row matching, table cell accuracy | |
| Provenance | page accuracy, evidence verified rate, bbox IoU where available | Hallucination proxy: fields with `NOT_FOUND` evidence that were wrong |
| Normalization | accuracy per type; ambiguous-date `UNCERTAIN` rate | |
| Comparison & rules | discrepancy detection precision/recall/F1 per discrepancy code | Deterministic → also CI regression tests |
| Duplicates | precision/recall at chosen threshold | |
| Confidence routing | auto-processed share, **error rate inside auto bucket**, review rate | The metric that matters for operations |
| RAG retrieval | Recall@k, Precision@k, MRR, nDCG@k (k = 1, 3, 5, 10) | Ablations: dense / FTS / hybrid; chunk size; contextual prefix |
| RAG answers | citation precision/recall, unsupported-claim rate, refusal accuracy on unanswerable | LLM-judge results labelled as such, spot-checked by a human |
| Agent | task success (expected recommendation), tool-selection precision/recall vs expected tools, evidence grounding rate, injection resistance rate, steps, LLM calls | |
| Workflows | final proposal vs expected action (per expected approval / stop), unsafe proposals, maker-checker bypasses (service and database), audited refusals and state changes, report re-render and regeneration hashes, contract rule outcomes and version changes vs the generator's record, latency to proposal and of a decision | |
| System | p50/p95 latency per pipeline stage, documents/minute per worker, failure rate, LLM tokens & estimated cost per document | From `processing_jobs.stage_timings` + `llm_calls` |

## 3. Execution

* `make evaluate` → `docintel evaluate --suite all` → writes
  `evaluation/reports/<suite>.{json,md}` (stable names, git keeps history, ADR-027); the
  `evaluations` table arrives in Phase 10.
* CI runs the **deterministic** suites (normalization, comparison, rules,
  retrieval with a local embedding model) as regression gates.
* LLM-dependent suites run manually or on a schedule (free-tier quotas), with a
  budget cap and caching so re-runs are cheap.
* Threshold calibration (confidence weights, retrieval score threshold) is done on
  `dev`, then frozen and reported on `test`.

## 4. Status

| Suite | Since | Report | Datasets |
|---|---|---|---|
| OCR (CER, WER, word F1 per degradation + preprocessing ablation) | Phase 3 | [`evaluation/reports/ocr.md`](../../evaluation/reports/ocr.md) | `synthetic-noisy`: first page of every native synthetic-core document (seed 7) under six degradations |
| Classification (accuracy, macro-F1, per class, confusion, ECE, auto-accept error) | Phase 3 | [`evaluation/reports/classification.md`](../../evaluation/reports/classification.md) | held-out corpus text (seed 1001), corpus rendered to PDF and scanned (seed 2002), synthetic-core (seed 42) |
| Line-item tables (found, row P/R, cell accuracy per column) | Phase 3 | [`evaluation/reports/tables.md`](../../evaluation/reports/tables.md) | synthetic-core (seed 11), native, re-rendered as scans and the dataset's own scans |
| Structured extraction, layout extractor (field exact / normalized match, P/R/F1 per field, line-item row P/R and cell accuracy, vendor and date normalization of printed variants, consistency checks, auto-accept share and **error inside the auto bucket**) | Phase 4 | [`evaluation/reports/extraction.md`](../../evaluation/reports/extraction.md) | synthetic-core POs, invoices and delivery notes (seed 31), native, re-rendered as scans and the dataset's own scans; document type from ground truth |
| Discrepancies and duplicates (recall per planted defect, precision per rule, (document, rule) P/R/F1 at FAIL and at FAIL-or-WARN, alarms on defect-free documents, resent-invoice detection) — extraction as the worker does it, then `matching.service.assess`, all bundles of an input in one department | Phase 5 | [`evaluation/reports/discrepancies.md`](../../evaluation/reports/discrepancies.md) | synthetic-core, all 12 scenarios × 4 bundles (seed 53, 148 documents), as generated and with every native PDF re-rendered as a scan |
| Contract versions (clause segmentation; added / removed / modified P/R; steps exactly right) | Phase 5 | [`evaluation/reports/versions.md`](../../evaluation/reports/versions.md) | 20 synthetic contract families × 3 versions (seed 61), native and re-rendered as scans |
| Knowledge retrieval (hit@1/3/5, section recall@5, precision@5, MRR, nDCG@5; evidence-gate refusals and false refusals; access control; version filtering; latency; ablations dense / full text / hybrid, full-text order, contextual prefix off, fixed-size chunks) — the seed knowledge base ingested by the production upload service and worker in a scratch database, questions answered by `KnowledgeRetriever` | Phase 6 | [`evaluation/reports/retrieval.md`](../../evaluation/reports/retrieval.md) | `kb-queries` (tuning) and `kb-queries-holdout`; offline lexical hashing embeddings |
| Business document search (precision / recall / exact result sets per question family: vendor, payment terms, totals, dates, types, free text) — synthetic documents processed by the worker, questions generated from ground truth, answered by `DocumentSearchService` | Phase 6 | [`evaluation/reports/search.md`](../../evaluation/reports/search.md) | synthetic-core, 6 scenarios × 2 bundles (seed 2), native PDFs |
| Agent investigations (task success per way of asking, unsafe recommendations, planted defect reported, false failures on clean invoices, identification from the question, tool selection P/R, findings whose evidence exists, governing policy among the sources, policy-question sections, guardrails against a scripted adversarial model, tool calls and latency per run) — synthetic bundles and the seed knowledge base processed by the worker in a scratch database, every invoice investigated through the job queue named, found from the question and (if defective) with the adversary | Phase 7 | [`evaluation/reports/agent.md`](../../evaluation/reports/agent.md) | synthetic-scenarios, 10 invoice scenarios × 2 bundles: development (seed 7, used while building) and held-out (seed 11); 10 answerable `kb-queries-holdout` questions; deterministic mode |
| Workflow automation (proposal vs expected action, unsafe proposals, open-review holds, maker-checker probes through the service and the table, audited refusals and transitions, report re-render and regeneration, contract rule outcomes and version changes, latency) — every invoice and contract version processed by the worker in a scratch database, each workflow started by one manager and decided by another through the job queue and the service | Phase 8 | [`evaluation/reports/workflow.md`](../../evaluation/reports/workflow.md) | the agent suite's invoice datasets (seeds 7 and 11); `synthetic-contracts`, 12 families × 3 versions (seed 73); deterministic mode |

Not yet measured: the **LLM extraction path** (no API key or local model in the build
environment; its merge, evidence and gating logic is covered by tests), provenance metrics
(page accuracy, bbox IoU), field extraction for contracts, receipts, resumes, bank statements
and policies (no generator ground truth yet), contract/policy comparison, **semantic embeddings**
(Gemini, fastembed: no key or model download in the build environment — retrieval was measured
with the lexical hashing model), **generated RAG answers** (citation precision/recall,
unsupported-claim rate: need an LLM; the citation and grounding checks are covered by tests),
**model-assisted agent runs and workflow proposals** (planning and analysis with Gemini or a
local model: the validators and guardrails are measured against a scripted adversary instead)
and system latency. The retrieval, search, agent and workflow suites need a PostgreSQL server
(`TEST_DATABASE_URL`); they create and drop a scratch database. Comparison and rules are deterministic: besides the suite, every planted
discrepancy on native documents is a unit test (`tests/unit/test_rules.py`), so CI catches a
regression without running the evaluation. All current
datasets are synthetic; reports say so next to the numbers.

End to end (Phase 9, ADR-066): `make e2e` drives the demonstration path (master prompt §50)
through the web app in Chromium against a running stack, and CI runs it after the container
smoke test. It is a pass/fail check that the pieces work together the way a person uses them,
not a measurement; it generates a fresh bundle per run, so earlier data on the stack does not
change its outcome.
