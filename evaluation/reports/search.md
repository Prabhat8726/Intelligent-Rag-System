# Business document search evaluation

Generated 2026-10-10T14:15:51+00:00 from commit `2a74f53db57f` by `docintel evaluate --suite search`. Do not edit by hand.

## Results per question family

| family | questions | precision | recall | F1 | exact (text: all found) |
|---|---|---|---|---|---|
| vendor | 15 | 100.0% | 100.0% | 100.0% | 100.0% |
| payment terms | 3 | 100.0% | 100.0% | 100.0% | 100.0% |
| totals | 2 | 100.0% | 100.0% | 100.0% | 100.0% |
| dates | 4 | 100.0% | 100.0% | 100.0% | 100.0% |
| types | 2 | 100.0% | 100.0% | 100.0% | 100.0% |
| text | 6 | 52.5% | 100.0% | 68.8% | 100.0% |

## Questions with a different result set

| family | question | missing | unexpected |
|---|---|---|---|
| - | none | - | - |

## Notes

* Questions are generated from the ground truth of a synthetic dataset (native PDFs) and every document went through the production pipeline (classification, extraction, vendor resolution, indexing) first, so the scores measure parsing, extraction and filtering together.
* Structured families (vendor, payment terms, totals, dates, types) are scored as sets: precision and recall over all questions of the family, and the share of questions whose result set is exactly right.
* Text questions return a ranked list (first 10); documents that list the item are relevant. Other documents sharing words with the item are returned too, so text precision is low by design.
* The first run of this suite found vendor names containing 'and' cut short ('Harbor and Pine Packaging Ltd' read as 'Harbor'); the parser was fixed and the suite re-run. Questions come from the ground truth of clean native PDFs, so these structured-search scores are an upper bound: scanned documents are not included.
* Embeddings: lexical hashing model; semantic models not measured.

## Provenance

```json
{
  "dataset": {
    "generator_seed": 2,
    "scenarios": [
      "CLEAN_MATCH",
      "VENDOR_NAME_VARIANT",
      "UNIT_PRICE_MISMATCH",
      "QUANTITY_MISMATCH",
      "SHORT_DELIVERY",
      "TAX_RATE_MISMATCH"
    ],
    "documents": 18,
    "questions": 32
  },
  "config": {
    "embedding_model": "hashing-ngram-v1",
    "text_depth": 10,
    "quick": false,
    "seconds": 7.6
  },
  "environment": {
    "python": "3.13.16",
    "pypdfium2": "5.14.0",
    "pillow": "12.3.0",
    "scikit-learn": "1.9.1",
    "numpy": "2.5.3",
    "rapidfuzz": "3.14.6",
    "reportlab": "5.0.1",
    "embedding": "hashing-ngram-v1"
  }
}
```
