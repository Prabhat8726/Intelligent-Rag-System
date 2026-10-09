# Contract version comparison (clause changes)

Generated 2026-10-09T07:46:46+00:00 from commit `fa1fcce71bb1` by `docintel evaluate --suite versions`. Do not edit by hand.

## Summary

| Input | Steps | Steps exactly right | Change P / R | Added P / R | Removed P / R | Modified P / R | Segmentation (titles) |
|---|---|---|---|---|---|---|---|
| native | 40 | 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% |
| scanned (re-rendered) | 40 | 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% / 100.0% | 100.0% |

## Notes

* A step is exactly right when its added, removed and modified clauses are all found and nothing else is reported (renumbering alone is not a change).
* Titles are compared case-insensitively with a similarity of at least 90 (an OCR slip in a heading does not hide a correctly detected change).
* Synthetic contracts use one heading style ('N. Title'); other styles (Clause N, Article N, nested N.N) are covered by unit tests, not measured here.

## Provenance

```json
{
  "dataset": {
    "name": "synthetic-contract-versions",
    "seed": 61,
    "families": 20,
    "versions_per_family": 3,
    "steps": 40,
    "quick": false
  },
  "config": {
    "title_match_ratio": 90.0,
    "ocr_languages": "eng"
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
