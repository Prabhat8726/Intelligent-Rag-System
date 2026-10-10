# Workflow automation evaluation

Generated 2026-10-10T08:18:23+00:00 from commit `169718e79612` by `docintel evaluate --suite workflow`. Do not edit by hand.

## Invoice processing (development dataset)

| Measure | Value |
|---|---|
| Workflows started / completed | 22 / 22 |
| Final proposal as expected | 100.0% |
| ... when an approval was expected | 100.0% |
| ... when a stop was expected | 100.0% |
| Unsafe proposals (approval where a stop was expected) | 0 |
| Held at first because a review task was open (then resolved and re-run) | 0 |
| Proposals that needed a person's approval | 8 |
| Actions executed | 22 |
| Maker-checker attempts refused / bypasses | 40/40 / 0 |
| Refusals with an audit event | 100.0% |
| Action state changes with an audit event | 74/74 (100.0% of workflows) |
| Workflow report re-renders to its stored hash | 100.0% |
| Report generated twice: identical SHA-256 | 100.0% |
| Workflow run p50 / p95 (ms, start to proposal, includes the job queue) | 436.5 / 524.1 |
| Approval p50 / p95 (ms, decision, execution and report) | 117.0 / 208.4 |
| Workflow errors | 0 |

## Invoice processing (held-out dataset)

| Measure | Value |
|---|---|
| Workflows started / completed | 22 / 22 |
| Final proposal as expected | 100.0% |
| ... when an approval was expected | 100.0% |
| ... when a stop was expected | 100.0% |
| Unsafe proposals (approval where a stop was expected) | 0 |
| Held at first because a review task was open (then resolved and re-run) | 0 |
| Proposals that needed a person's approval | 8 |
| Actions executed | 22 |
| Maker-checker attempts refused / bypasses | 40/40 / 0 |
| Refusals with an audit event | 100.0% |
| Action state changes with an audit event | 74/74 (100.0% of workflows) |
| Workflow report re-renders to its stored hash | 100.0% |
| Report generated twice: identical SHA-256 | 100.0% |
| Workflow run p50 / p95 (ms, start to proposal, includes the job queue) | 423.2 / 551.3 |
| Approval p50 / p95 (ms, decision, execution and report) | 126.5 / 163.6 |
| Workflow errors | 0 |

## Contract review (generator seed 73, three versions per contract)

| Measure | Value |
|---|---|
| Workflows started / completed | 36 / 36 |
| Final proposal as expected | 100.0% |
| ... when an approval was expected | 100.0% |
| ... when a stop was expected | 100.0% |
| Unsafe proposals (approval where a stop was expected) | 0 |
| Contract rule outcomes as expected | 144/144 (100.0%) |
| Version comparison step: exact change lists / precision / recall | 100.0% / 100.0% / 100.0% (24 steps) |
| Held at first because a review task was open (then resolved and re-run) | 4 |
| Proposals that needed a person's approval | 8 |
| Actions executed | 36 |
| Maker-checker attempts refused / bypasses | 40/40 / 0 |
| Refusals with an audit event | 100.0% |
| Action state changes with an audit event | 128/128 (100.0% of workflows) |
| Workflow report re-renders to its stored hash | 100.0% |
| Report generated twice: identical SHA-256 | 100.0% |
| Workflow run p50 / p95 (ms, start to proposal, includes the job queue) | 408.0 / 466.5 |
| Approval p50 / p95 (ms, decision, execution and report) | 119.2 / 133.2 |
| Workflow errors | 0 |

## Invoice processing per scenario (development)

| Scenario | Workflows | Expected | Proposed | Correct | Outcome |
|---|---|---|---|---|---|
| CLEAN_MATCH | 2 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2 | 100.0% | APPROVED_FOR_PAYMENT x2 |
| DUPLICATE_INVOICE (copy) | 2 | REJECT_DUPLICATE | REJECT_DUPLICATE x2 | 100.0% | REJECTED_AS_DUPLICATE x2 |
| DUPLICATE_INVOICE (original) | 2 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2 | 100.0% | APPROVED_FOR_PAYMENT x2 |
| MISSING_PO_REFERENCE | 2 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| QUANTITY_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| SHORT_DELIVERY | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| TAX_RATE_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| TOTAL_ARITHMETIC_ERROR | 2 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| UNIT_PRICE_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| VENDOR_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| VENDOR_NAME_VARIANT | 2 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2 | 100.0% | APPROVED_FOR_PAYMENT x2 |

## Invoice processing per scenario (held-out)

| Scenario | Workflows | Expected | Proposed | Correct | Outcome |
|---|---|---|---|---|---|
| CLEAN_MATCH | 2 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2 | 100.0% | APPROVED_FOR_PAYMENT x2 |
| DUPLICATE_INVOICE (copy) | 2 | REJECT_DUPLICATE | REJECT_DUPLICATE x2 | 100.0% | REJECTED_AS_DUPLICATE x2 |
| DUPLICATE_INVOICE (original) | 2 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2 | 100.0% | APPROVED_FOR_PAYMENT x2 |
| MISSING_PO_REFERENCE | 2 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| QUANTITY_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| SHORT_DELIVERY | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| TAX_RATE_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| TOTAL_ARITHMETIC_ERROR | 2 | HOLD_FOR_REVIEW | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| UNIT_PRICE_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| VENDOR_MISMATCH | 2 | HOLD_FOR_REVIEW/REQUEST_VENDOR_CLARIFICATION | HOLD_FOR_REVIEW x2 | 100.0% | SENT_TO_REVIEW x2 |
| VENDOR_NAME_VARIANT | 2 | APPROVE_FOR_PAYMENT | APPROVE_FOR_PAYMENT x2 | 100.0% | APPROVED_FOR_PAYMENT x2 |

## Contract review per ground truth

| Contract version | Workflows | Expected | Proposed | Correct | Outcome |
|---|---|---|---|---|---|
| deviates | 28 | REQUEST_LEGAL_REVIEW | REQUEST_LEGAL_REVIEW x28 | 100.0% | SENT_TO_LEGAL_REVIEW x28 |
| follows the guidelines | 8 | APPROVE_CONTRACT | APPROVE_CONTRACT x8 | 100.0% | CONTRACT_APPROVED x8 |

## Contract rules per version

| Rule | As expected | Expected outcomes |
|---|---|---|
| CONTRACT_EXPIRY | 36/36 | PASS x36 |
| CONTRACT_GOVERNING_LAW | 36/36 | NOT_APPLICABLE x4, PASS x12, WARN x20 |
| CONTRACT_REQUIRED_CLAUSES | 36/36 | FAIL x8, PASS x28 |
| CONTRACT_TERMINATION_NOTICE | 36/36 | FAIL x9, NOT_APPLICABLE x2, PASS x25 |

## Workflows with an unexpected proposal

| Dataset | Case | Expected | Proposed | Rule differences / errors |
|---|---|---|---|---|
| - | - | - | - | - |

## Notes

* Expected outcomes come from the generator's ground truth (planted invoice defects; the clauses, notice period, governing law and expiry each contract version was written with), not from the system.
* An approval is never proposed while the document has an open review task; such proposals are counted, the task is resolved by a second reviewer and the workflow runs again. The scores use the final run.
* Maker-checker probes go through the same service the API calls; the direct table write checks the database constraint behind it.
* The contract generator was changed in this phase (guideline ground truth; half of the contracts under the company's own law; notice periods up to 120 days); seed 73 was not used while developing the contract rules.
* The documents are synthetic and come from the templates the extractors and rules were developed on: these figures show that the workflows and their controls behave as designed end to end, not accuracy on real-world documents.
* REQUEST_VENDOR_CLARIFICATION is only proposed by a model (the deterministic analysis holds such invoices for review); it is covered by an integration test with a scripted model, not here. No contract in this dataset expires within 30 days of the reference date, so the expiry rule is only exercised as PASS here (its other outcomes are unit-tested).
* Model-assisted proposals (Gemini or a local model): Not yet measured - no model was available in the build environment.
* Run time: 118.3 s.

## Provenance

```json
{
  "dataset": {
    "invoice_development_seed": 7,
    "invoice_held_out_seed": 11,
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
    "contract_seed": 73,
    "contract_families": 12,
    "contract_versions": 36,
    "reference_date": "2026-10-01"
  },
  "config": {
    "mode": "deterministic (no LLM): keyword planner, rule-based analysis and proposals",
    "embedding": "hashing (offline)",
    "people": "uploader (reviewer), starter (manager), approver (manager), second reviewer; all in Finance",
    "workflow_definitions": {
      "INVOICE_PROCESSING": 1,
      "CONTRACT_REVIEW": 1
    }
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
