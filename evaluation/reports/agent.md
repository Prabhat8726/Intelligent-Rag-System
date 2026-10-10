# Agent investigation evaluation

Generated 2026-10-10T13:10:53+00:00 from commit `fa098dfb7c7c` by `docintel evaluate --suite agent`. Do not edit by hand.

## Overall (development dataset)

| Measure | Value |
|---|---|
| Runs completed | 70/70 |
| Task success, document named | 100.0% |
| Task success, document found from the question | 100.0% |
| Task success, policy questions | 100.0% |
| Unsafe recommendations (payment of a defective invoice) | 0 |
| Planted defect reported as a rule finding | 100.0% |
| False rule failures on clean invoices | 0 |
| Target invoice identified from the question | 100.0% |
| ... and nothing else | 100.0% |
| Tool selection precision / recall | 99.2% / 100.0% |
| Findings whose evidence labels all exist | 236/236 (100.0%) |
| Findings citing nothing (by category) | 0 |
| Governing policy among the sources (defective invoices) | 85.7% |
| Policy questions: expected section among the sources | 100.0% |
| Tool calls per document run | 7.95 |
| Latency per run p50 / p95 (ms, includes the job queue) | 347.9 / 509.0 |
| LLM calls in deterministic runs | 0 |

## Overall (held-out dataset, generator seed 11, never used while developing)

| Measure | Value |
|---|---|
| Runs completed | 60/60 |
| Task success, document named | 100.0% |
| Task success, document found from the question | 100.0% |
| Unsafe recommendations (payment of a defective invoice) | 0 |
| Planted defect reported as a rule finding | 100.0% |
| False rule failures on clean invoices | 0 |
| Target invoice identified from the question | 100.0% |
| ... and nothing else | 100.0% |
| Tool selection precision / recall | 99.2% / 100.0% |
| Findings whose evidence labels all exist | 206/206 (100.0%) |
| Findings citing nothing (by category) | 0 |
| Governing policy among the sources (defective invoices) | 85.7% |
| Tool calls per document run | 7.95 |
| Latency per run p50 / p95 (ms, includes the job queue) | 430.0 / 590.0 |
| LLM calls in deterministic runs | 0 |

## Guardrails against a scripted adversarial model (development)

| Measure | Value |
|---|---|
| Runs (defective invoices) | 16 |
| Payment recommendations accepted | 0 |
| Recommendation still correct | 100.0% |
| Adversarial statements kept as findings | 0 |
| Adversarial summaries kept | 0 |

## Guardrails against a scripted adversarial model (held-out)

| Measure | Value |
|---|---|
| Runs (defective invoices) | 16 |
| Payment recommendations accepted | 0 |
| Recommendation still correct | 100.0% |
| Adversarial statements kept as findings | 0 |
| Adversarial summaries kept | 0 |

## Per scenario, named and search runs (development)

| Scenario | Runs | Expected | Recommended | Correct | Defect found | Policy found |
|---|---|---|---|---|---|---|
| CLEAN_MATCH (INV) | 4 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x4 | 100.0% | n/a | n/a |
| DUPLICATE_INVOICE (INV) | 4 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2, REJECT_DUPLICATE x2 | 100.0% | n/a | n/a |
| DUPLICATE_INVOICE (INV2) | 4 | REJECT_DUPLICATE | REJECT_DUPLICATE x4 | 100.0% | 100.0% | 100.0% |
| MISSING_PO_REFERENCE (INV) | 4 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 100.0% |
| QUANTITY_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 100.0% |
| SHORT_DELIVERY (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 100.0% |
| TAX_RATE_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | REQUEST_VENDOR_CLARIFICATION x4 | 100.0% | 100.0% | 100.0% |
| TOTAL_ARITHMETIC_ERROR (INV) | 4 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | n/a |
| UNIT_PRICE_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | REQUEST_VENDOR_CLARIFICATION x4 | 100.0% | 100.0% | 100.0% |
| VENDOR_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 0.0% |
| VENDOR_NAME_VARIANT (INV) | 4 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x4 | 100.0% | n/a | n/a |

## Per scenario, named and search runs (held-out)

| Scenario | Runs | Expected | Recommended | Correct | Defect found | Policy found |
|---|---|---|---|---|---|---|
| CLEAN_MATCH (INV) | 4 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x4 | 100.0% | n/a | n/a |
| DUPLICATE_INVOICE (INV) | 4 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2, REJECT_DUPLICATE x2 | 100.0% | n/a | n/a |
| DUPLICATE_INVOICE (INV2) | 4 | REJECT_DUPLICATE | REJECT_DUPLICATE x4 | 100.0% | 100.0% | 100.0% |
| MISSING_PO_REFERENCE (INV) | 4 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 100.0% |
| QUANTITY_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 100.0% |
| SHORT_DELIVERY (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 100.0% |
| TAX_RATE_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | REQUEST_VENDOR_CLARIFICATION x4 | 100.0% | 100.0% | 100.0% |
| TOTAL_ARITHMETIC_ERROR (INV) | 4 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | n/a |
| UNIT_PRICE_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | REQUEST_VENDOR_CLARIFICATION x4 | 100.0% | 100.0% | 100.0% |
| VENDOR_MISMATCH (INV) | 4 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x4 | 100.0% | 100.0% | 0.0% |
| VENDOR_NAME_VARIANT (INV) | 4 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x4 | 100.0% | n/a | n/a |

## Runs with an unexpected recommendation

| Dataset | Case | Question | Expected | Recommended | Rule failures |
|---|---|---|---|---|---|
| - | - | - | - | - | - |

## Notes

* Expected outcomes come from the generator's ground truth (planted defects), not from the system. A run that holds a clean invoice because extraction or a rule raised a false warning counts as a miss.
* The development dataset was used while building the agent: it exposed and drove fixes to identification by document number, the keyword planner (questions without a document; team names read as vendors) and a guardrail. The held-out dataset uses another generator seed and was only run afterwards.
* Policy retrieval for vendor mismatches misses the vendor section with the offline lexical (hashing) embeddings: the rule name shares most words with purchase-order sections. A semantic embedding model is expected to help; not yet measured.
* Model-assisted planning and analysis (Gemini or a local model): Not yet measured - no model was available in the build environment.
* Guardrail figures use a scripted adversary, so they measure the validators and guardrails, not an LLM's behaviour.
* Run time: 105.0 s.

## Provenance

```json
{
  "dataset": {
    "development_seed": 7,
    "held_out_seed": 11,
    "bundles_per_scenario": 2,
    "scenarios": [
      "CLEAN_MATCH",
      "VENDOR_NAME_VARIANT",
      "UNIT_PRICE_MISMATCH",
      "QUANTITY_MISMATCH",
      "SHORT_DELIVERY",
      "MISSING_PO_REFERENCE",
      "TOTAL_ARITHMETIC_ERROR",
      "TAX_RATE_MISMATCH",
      "VENDOR_MISMATCH",
      "DUPLICATE_INVOICE"
    ],
    "invoices": {
      "development": 22,
      "held-out": 22
    },
    "policy_questions": 10,
    "policy_question_source": "kb-queries-holdout.json (not used for retrieval tuning)",
    "knowledge_base": "knowledge_base/*.md (seed), versions in force on 2026-10-01"
  },
  "config": {
    "mode": "deterministic (no LLM): keyword planner, rule-based analysis",
    "embedding": "hashing (offline)",
    "agent_max_tool_calls": 30,
    "adversarial_model": "scripted (not an LLM): proposes payment, claims all passed"
  },
  "environment": {
    "python": "3.13.16",
    "pypdfium2": "5.14.0",
    "pillow": "12.3.0",
    "scikit-learn": "1.9.1",
    "numpy": "2.5.3",
    "rapidfuzz": "3.14.6",
    "reportlab": "5.0.1"
  }
}
```
