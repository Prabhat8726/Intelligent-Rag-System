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

Everything else (extraction, provenance, comparison, RAG, agent, system latency): **Not yet
measured.** All current datasets are synthetic; reports say so next to the numbers.
