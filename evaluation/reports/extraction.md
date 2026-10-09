# Structured extraction (layout extractor, no LLM)

Generated 2026-10-09T01:38:42+00:00 from commit `44c14e2ea276` by `docintel evaluate --suite extraction`. Do not edit by hand.

## Summary by input

| Input | Docs | Field exact | Field normalized | Field P | Field R | Field F1 | Required all correct | Docs fully correct |
|---|---|---|---|---|---|---|---|---|
| native | 70 | 100.0% | 100.0% | 1.000 | 1.000 | 1.000 | 100.0% | 100.0% |
| scanned (dataset) | 4 | 91.7% | 100.0% | 1.000 | 1.000 | 1.000 | 100.0% | 25.0% |
| scanned (re-rendered) | 70 | 95.9% | 99.3% | 0.999 | 0.993 | 0.996 | 97.1% | 22.9% |

## Fields: normalized match / F1

| Field | native (norm. / F1) | scanned (dataset) (norm. / F1) | scanned (re-rendered) (norm. / F1) |
|---|---|---|---|
| buyer_name | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| currency | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| document date | 100.0% / 1.000 | 100.0% / 1.000 | 98.6% / 0.993 |
| document number | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| due_date | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| payment_terms_days | 100.0% / 1.000 | 100.0% / 1.000 | 97.9% / 0.990 |
| purchase_order_number | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| subtotal | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| tax_amount | 100.0% / 1.000 | 100.0% / 1.000 | 97.9% / 0.990 |
| tax_rate | 100.0% / 1.000 | 100.0% / 1.000 | 97.9% / 0.990 |
| total | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |
| vendor_name | 100.0% / 1.000 | 100.0% / 1.000 | 98.6% / 0.986 |
| vendor_tax_id | 100.0% / 1.000 | 100.0% / 1.000 | 100.0% / 1.000 |

## Line items

| Input | Row P | Row R | Row F1 | Cell accuracy | line_number | sku | description | quantity | unit | unit_price | amount |
|---|---|---|---|---|---|---|---|---|---|---|---|
| native | 1.000 | 1.000 | 1.000 | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| scanned (dataset) | 1.000 | 1.000 | 1.000 | 86.5% | 75.0% | 62.5% | 93.8% | 100.0% | 87.5% | 100.0% | 100.0% |
| scanned (re-rendered) | 0.940 | 0.738 | 0.827 | 78.1% | 45.2% | 76.1% | 88.5% | 81.8% | 82.1% | 86.1% | 93.6% |

## Confidence routing

| Input | Auto-accepted | Error in auto bucket | Analyst review | Mandatory review | High-confidence fields | Error in high-confidence fields |
|---|---|---|---|---|---|---|
| native | 97.1% | 0.0% | 0 | 2 | 682 | 0.0% |
| scanned (dataset) | 0.0% | n/a | 1 | 3 | 30 | 0.0% |
| scanned (re-rendered) | 0.0% | n/a | 47 | 23 | 592 | 0.0% |

## By input and document type

| Input / type | Docs | Field normalized | Field F1 | Row F1 | Cell accuracy | Auto-accepted | Error in auto |
|---|---|---|---|---|---|---|---|
| native / DELIVERY_NOTE | 22 | 100.0% | 1.000 | 1.000 | 100.0% | 100.0% | 0.0% |
| native / INVOICE | 24 | 100.0% | 1.000 | 1.000 | 100.0% | 91.7% | 0.0% |
| native / PURCHASE_ORDER | 24 | 100.0% | 1.000 | 1.000 | 100.0% | 100.0% | 0.0% |
| scanned (dataset) / DELIVERY_NOTE | 2 | 100.0% | 1.000 | 1.000 | 90.0% | 0.0% | n/a |
| scanned (dataset) / INVOICE | 2 | 100.0% | 1.000 | 1.000 | 83.9% | 0.0% | n/a |
| scanned (re-rendered) / DELIVERY_NOTE | 22 | 99.1% | 0.991 | 0.768 | 67.9% | 0.0% | n/a |
| scanned (re-rendered) / INVOICE | 24 | 100.0% | 1.000 | 0.677 | 75.8% | 0.0% | n/a |
| scanned (re-rendered) / PURCHASE_ORDER | 24 | 98.5% | 0.992 | 1.000 | 83.8% | 0.0% | n/a |

## Consistency checks: documents flagged inconsistent

| Printed document | Docs | Flagged |
|---|---|---|
| printed document consistent (flag = misread) | 140 | 1.4% |
| printed total wrong | 4 | 100.0% |

## Normalization of printed variants

| Input / variation | Docs | Correct |
|---|---|---|
| native / DATE_FORMAT_VARIANT | 2 | 100.0% |
| native / VENDOR_NAME_VARIANT | 2 | 100.0% |
| scanned (re-rendered) / DATE_FORMAT_VARIANT | 2 | 100.0% |
| scanned (re-rendered) / VENDOR_NAME_VARIANT | 2 | 100.0% |

## Notes

* Synthetic documents from one generator with three templates. The layout extractor's label vocabulary was written with these templates visible, so these numbers measure the pipeline, not generalization to unseen layouts.
* Exact = the extracted text equals the printed text (whitespace collapsed); normalized = the typed value equals the truth (Decimal amounts, ISO dates, vendor resolved to the canonical vendor). Precision/recall count a wrong value as both a false positive and a false negative; nulls are explicit.
* Fully correct = every header field and every line-item cell right. 'Error in auto bucket' is the share of auto-accepted documents with at least one error: the number that matters operationally.
* LLM extraction: Not yet measured (no GEMINI_API_KEY or local model in the build environment). Its merge, verification and gating logic is covered by tests.
* Document type taken from ground truth (classification is measured separately).

## Provenance

```json
{
  "dataset": {
    "name": "synthetic-core",
    "seed": 31,
    "documents": 74,
    "bundles_per_scenario": 2,
    "scanned_rerender": "light scan profile at 150 DPI",
    "vendor_master": "demo vendors: canonical names and tax IDs, no name variants"
  },
  "config": {
    "engine": "tesseract 5.3.4",
    "languages": "eng",
    "quick": false,
    "llm": "not used (EXTRACTION_LLM_MODE=never)",
    "thresholds": {
      "high": 0.85,
      "medium": 0.6
    }
  },
  "environment": {
    "python": "3.13.16",
    "pypdfium2": "5.14.0",
    "pillow": "12.3.0",
    "scikit-learn": "1.9.1",
    "numpy": "2.5.3",
    "rapidfuzz": "3.14.6",
    "reportlab": "5.0.1",
    "tesseract": "tesseract 5.3.4"
  }
}
```
