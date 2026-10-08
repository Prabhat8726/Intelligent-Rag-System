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
| `kb-queries` | Hand-written questions over the seeded policy KB | Relevant chunk ids / section paths; answerable flag | 6 |
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
| System | p50/p95 latency per pipeline stage, documents/minute per worker, failure rate, LLM tokens & estimated cost per document | From `processing_jobs.stage_timings` + `llm_calls` |

## 3. Execution

* `make evaluate` → `python -m docintel.evaluation run --suite all` → writes
  `evaluation/reports/<timestamp>-<suite>.{json,md}` and an `evaluations` row.
* CI runs the **deterministic** suites (normalization, comparison, rules,
  retrieval with a local embedding model) as regression gates.
* LLM-dependent suites run manually or on a schedule (free-tier quotas), with a
  budget cap and caching so re-runs are cheap.
* Threshold calibration (confidence weights, retrieval score threshold) is done on
  `dev`, then frozen and reported on `test`.

## 4. Phase 0 status

All metrics: **Not yet measured.** Phase 0 delivers the evaluation design, the
`evaluations` table design and the test infrastructure; datasets arrive with the
generator in Phase 2.
