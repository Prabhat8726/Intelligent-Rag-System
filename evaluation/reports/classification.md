# Document classification

Generated 2026-10-09T01:30:59+00:00 from commit `8ceaf1a0a0ac` by `docintel evaluate --suite classification`. Do not edit by hand.

## Summary

| Evaluation set | n | Accuracy | Macro F1 | ECE | Auto-accepted | Error in auto bucket |
|---|---|---|---|---|---|---|
| text/as_generated | 900 | 100.0% | 1.000 | 0.020 | 100.0% | 0.0% |
| text/heavy_ocr_noise | 900 | 100.0% | 1.000 | 0.023 | 100.0% | 0.0% |
| text/first_300_chars | 900 | 99.7% | 0.997 | 0.059 | 97.0% | 0.0% |
| rendered/native | 45 | 100.0% | 1.000 | 0.018 | 100.0% | 0.0% |
| rendered/scanned | 45 | 100.0% | 1.000 | 0.014 | 100.0% | 0.0% |
| business/native | 35 | 100.0% | 1.000 | 0.081 | 100.0% | 0.0% |
| business/scanned | 2 | 100.0% | 1.000 | 0.072 | 100.0% | 0.0% |
| rendered/all | 90 | 100.0% | 1.000 | 0.016 | 100.0% | 0.0% |

## Per class (rendered, all nine types)

| Type | Support | Precision | Recall | F1 |
|---|---|---|---|---|
| INVOICE | 10 | 1.000 | 1.000 | 1.000 |
| PURCHASE_ORDER | 10 | 1.000 | 1.000 | 1.000 |
| CONTRACT | 10 | 1.000 | 1.000 | 1.000 |
| RECEIPT | 10 | 1.000 | 1.000 | 1.000 |
| DELIVERY_NOTE | 10 | 1.000 | 1.000 | 1.000 |
| RESUME | 10 | 1.000 | 1.000 | 1.000 |
| BANK_STATEMENT | 10 | 1.000 | 1.000 | 1.000 |
| POLICY | 10 | 1.000 | 1.000 | 1.000 |
| OTHER | 10 | 1.000 | 1.000 | 1.000 |

## Confusion matrix (rendered; rows = truth)

| Truth | INVOICE | PURCHASE_ORDER | CONTRACT | RECEIPT | DELIVERY_NOTE | RESUME | BANK_STATEMENT | POLICY | OTHER |
|---|---|---|---|---|---|---|---|---|---|
| INVOICE | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| PURCHASE_ORDER | 0 | 10 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| CONTRACT | 0 | 0 | 10 | 0 | 0 | 0 | 0 | 0 | 0 |
| RECEIPT | 0 | 0 | 0 | 10 | 0 | 0 | 0 | 0 | 0 |
| DELIVERY_NOTE | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 | 0 |
| RESUME | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 0 | 0 |
| BANK_STATEMENT | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 0 |
| POLICY | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 10 | 0 |
| OTHER | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 10 |

## Notes

* Training and evaluation data are synthetic and come from related generators: near-perfect scores here do NOT predict accuracy on real documents. The business dataset uses a different generator than the training corpus but is still synthetic.
* Auto-accepted = local confidence >= threshold (no review); 'error in auto bucket' is the share of those that are wrong - the number that matters operationally.
* LLM fallback: not measured - no GEMINI_API_KEY in the build environment. Its logic is covered by tests with a fake provider.

## Provenance

```json
{
  "dataset": {
    "training": {
      "corpus_seed": 1,
      "per_class": 200
    },
    "text_held_out": {
      "seed": 1001,
      "documents": 900
    },
    "rendered": {
      "seed": 2002,
      "documents": 90,
      "scanned_share": 0.5
    },
    "business": {
      "seed": 42,
      "documents": 37
    }
  },
  "config": {
    "model_version": "tfidf-lr-v1:33c72e7df5fbff30",
    "threshold": 0.7,
    "engine": "tesseract 5.3.4",
    "languages": "eng",
    "quick": false
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
