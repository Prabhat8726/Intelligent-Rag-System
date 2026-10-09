# Line-item table extraction

Generated 2026-10-09T07:31:19+00:00 from commit `a22f1b8afcf8` by `docintel evaluate --suite tables`. Do not edit by hand.

## By input

| Input | Docs | Table found | Rows exact | Row P | Row R | Cell accuracy |
|---|---|---|---|---|---|---|
| native | 70 | 100.0% | 100.0% | 1.000 | 1.000 | 100.0% |
| scanned (dataset) | 4 | 75.0% | 75.0% | 0.286 | 0.200 | 100.0% |
| scanned (re-rendered) | 70 | 94.3% | 88.6% | 0.956 | 0.894 | 83.8% |

## By input and template

| Input | Docs | Table found | Rows exact | Row P | Row R | Cell accuracy |
|---|---|---|---|---|---|---|
| native / classic | 20 | 100.0% | 100.0% | 1.000 | 1.000 | 100.0% |
| native / compact | 27 | 100.0% | 100.0% | 1.000 | 1.000 | 100.0% |
| native / modern | 23 | 100.0% | 100.0% | 1.000 | 1.000 | 100.0% |
| scanned (dataset) / classic | 1 | 100.0% | 100.0% | 0.000 | 0.000 | 0.0% |
| scanned (dataset) / compact | 2 | 50.0% | 50.0% | 1.000 | 0.400 | 100.0% |
| scanned (dataset) / modern | 1 | 100.0% | 100.0% | 0.000 | 0.000 | 0.0% |
| scanned (re-rendered) / classic | 20 | 85.0% | 70.0% | 0.880 | 0.746 | 85.9% |
| scanned (re-rendered) / compact | 27 | 96.3% | 92.6% | 0.970 | 0.890 | 82.1% |
| scanned (re-rendered) / modern | 23 | 100.0% | 100.0% | 0.960 | 0.960 | 85.5% |

## Cell accuracy by column

| Input | number | sku | description | quantity | unit | unit_price | amount |
|---|---|---|---|---|---|---|---|
| native | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| scanned (dataset) | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% | 100.0% |
| scanned (re-rendered) | 72.9% | 78.2% | 90.6% | 82.3% | 88.1% | 88.3% | 88.6% |

## Notes

* Rows are paired with ground truth by SKU similarity (ratio >= 75); cell accuracy is exact match after whitespace/case normalization, on paired rows.
* Templates: classic (grid lines), modern (header rule, banded rows), compact (row rules, stacked header).
* Synthetic layouts are regular; real-world tables (merged cells, rotated headers, handwriting) will score lower.

## Provenance

```json
{
  "dataset": {
    "name": "synthetic-core",
    "seed": 11,
    "documents": 74,
    "bundles_per_scenario": 2,
    "scanned_rerender": "light scan profile at 150 DPI"
  },
  "config": {
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
