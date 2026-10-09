# OCR evaluation (synthetic-noisy)

Generated 2026-10-09T07:27:13+00:00 from commit `a22f1b8afcf8` by `docintel evaluate --suite ocr`. Do not edit by hand.

## Default pipeline

| Degradation | Variant | Pages | CER | WER | Word F1 | OCR conf | s/page |
|---|---|---|---|---|---|---|---|
| clean_300dpi | default | 35 | 1.2% | 1.5% | 0.989 | 94.8 | 1.77 |
| light_scan_150dpi | default | 35 | 2.3% | 3.2% | 0.980 | 93.9 | 2.36 |
| heavy_scan_150dpi | default | 35 | 10.4% | 14.9% | 0.891 | 89.2 | 2.28 |
| low_res_100dpi | default | 35 | 1.8% | 3.5% | 0.972 | 93.8 | 1.22 |
| skew_3deg | default | 35 | 2.0% | 3.2% | 0.977 | 93.5 | 2.47 |
| sideways_90deg | default | 35 | 2.7% | 3.5% | 0.980 | 93.7 | 6.05 |

## Preprocessing ablation

| Degradation | Variant | Pages | CER | WER | Word F1 | OCR conf | s/page |
|---|---|---|---|---|---|---|---|
| light_scan_150dpi | default | 35 | 2.3% | 3.2% | 0.980 | 93.9 | 2.36 |
| light_scan_150dpi | no_upscale | 35 | 3.1% | 5.3% | 0.965 | 92.3 | 1.30 |
| light_scan_150dpi | no_deskew | 35 | 2.2% | 3.4% | 0.979 | 94.0 | 2.10 |
| light_scan_150dpi | remove_ruling_lines | 35 | 2.4% | 3.5% | 0.976 | 93.4 | 3.19 |
| heavy_scan_150dpi | default | 35 | 10.4% | 14.9% | 0.891 | 89.2 | 2.28 |
| heavy_scan_150dpi | no_upscale | 35 | 8.7% | 15.4% | 0.882 | 88.3 | 1.21 |
| heavy_scan_150dpi | no_deskew | 35 | 11.3% | 16.0% | 0.884 | 89.3 | 1.98 |
| heavy_scan_150dpi | remove_ruling_lines | 35 | 14.9% | 20.2% | 0.853 | 87.8 | 3.19 |
| low_res_100dpi | default | 35 | 1.8% | 3.5% | 0.972 | 93.8 | 1.22 |
| low_res_100dpi | no_upscale | 35 | 7.1% | 18.7% | 0.843 | 78.9 | 0.67 |
| low_res_100dpi | no_deskew | 35 | 1.8% | 3.5% | 0.972 | 93.8 | 1.22 |
| low_res_100dpi | remove_ruling_lines | 35 | 2.1% | 3.9% | 0.970 | 93.3 | 1.48 |
| skew_3deg | default | 35 | 2.0% | 3.2% | 0.977 | 93.5 | 2.47 |
| skew_3deg | no_upscale | 35 | 1.9% | 3.0% | 0.977 | 93.4 | 1.43 |
| skew_3deg | no_deskew | 35 | 3.7% | 9.8% | 0.926 | 90.6 | 1.88 |
| skew_3deg | remove_ruling_lines | 35 | 2.0% | 3.2% | 0.977 | 93.5 | 3.31 |

## Notes

* Reference text is the PDF text layer of the same page; reference and OCR output are serialized by the same line builder (top-to-bottom, left-to-right).
* CER/WER are edit distances divided by reference length; word F1 is order-insensitive.
* Synthetic pages are cleaner than real scans (fonts, layout, no handwriting, no stamps): these numbers overstate real-world accuracy.

## Provenance

```json
{
  "dataset": {
    "name": "synthetic-noisy",
    "source": "synthetic-core generator, first page of each native document",
    "seed": 7,
    "documents": 35,
    "degradations": {
      "clean_300dpi": "rendered at 300 DPI, no noise",
      "light_scan_150dpi": "150 DPI, rotation <=0.8 deg, blur 0.4, noise 4%, JPEG q80 (dataset 'light' scans)",
      "heavy_scan_150dpi": "150 DPI, rotation <=2 deg, blur 1.0, noise 10%, JPEG q45",
      "low_res_100dpi": "rendered at 100 DPI, no noise",
      "skew_3deg": "200 DPI rotated by 3 degrees",
      "sideways_90deg": "light scan turned 90 degrees (orientation detection)"
    }
  },
  "config": {
    "engine": "tesseract 5.3.4",
    "languages": "eng",
    "extraction": {
      "ocr_dpi": 300,
      "upscale_below_dpi": 250,
      "remove_ruling_lines": false,
      "retry_orientation_below": 60.0,
      "deskew": true
    },
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
