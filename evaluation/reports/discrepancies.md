# Discrepancy and duplicate detection

Generated 2026-10-09T07:45:38+00:00 from commit `fa1fcce71bb1` by `docintel evaluate --suite discrepancies`. Do not edit by hand.

## Overall

| Input | Docs | P / R (fail) | P / R (fail or warn) | Defect-free flagged (fail) | Defect-free flagged (fail or warn) | Defective flagged | Defect-free in extraction review |
|---|---|---|---|---|---|---|---|
| as generated | 148 | 100.0% / 100.0% | 87.8% / 100.0% | 0.0% | 1.7% | 100.0% | 6.9% |
| all scanned | 148 | 80.0% / 77.8% | 45.3% / 94.4% | 4.3% | 16.4% | 96.9% | 98.3% |

## Detection by planted defect

| Input | Defect | Rule(s) | Detected (fail) | Detected (fail or warn) |
|---|---|---|---|---|
| as generated | BILLED_QUANTITY_EXCEEDS_DELIVERED | INV_DELIVERED_QUANTITY | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | DUPLICATE_INVOICE | INV_DUPLICATE | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | MISSING_PO_REFERENCE | INV_MISSING_PO | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | QUANTITY_MISMATCH | INV_DELIVERED_QUANTITY, INV_PO_QUANTITY | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | TAX_RATE_MISMATCH | INV_PO_TAX_RATE | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | TOTAL_MISMATCH | DOC_ARITHMETIC | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | UNIT_PRICE_MISMATCH | INV_PO_UNIT_PRICE | 4/4 (100.0%) | 4/4 (100.0%) |
| as generated | VENDOR_MISMATCH | INV_PO_VENDOR | 4/4 (100.0%) | 4/4 (100.0%) |
| all scanned | BILLED_QUANTITY_EXCEEDS_DELIVERED | INV_DELIVERED_QUANTITY | 0/4 (0.0%) | 4/4 (100.0%) |
| all scanned | DUPLICATE_INVOICE | INV_DUPLICATE | 4/4 (100.0%) | 4/4 (100.0%) |
| all scanned | MISSING_PO_REFERENCE | INV_MISSING_PO | 4/4 (100.0%) | 4/4 (100.0%) |
| all scanned | QUANTITY_MISMATCH | INV_DELIVERED_QUANTITY, INV_PO_QUANTITY | 4/4 (100.0%) | 4/4 (100.0%) |
| all scanned | TAX_RATE_MISMATCH | INV_PO_TAX_RATE | 2/4 (50.0%) | 3/4 (75.0%) |
| all scanned | TOTAL_MISMATCH | DOC_ARITHMETIC | 4/4 (100.0%) | 4/4 (100.0%) |
| all scanned | UNIT_PRICE_MISMATCH | INV_PO_UNIT_PRICE | 3/4 (75.0%) | 3/4 (75.0%) |
| all scanned | VENDOR_MISMATCH | INV_PO_VENDOR | 4/4 (100.0%) | 4/4 (100.0%) |

## Alarms per rule (correct / raised)

| Input | Rule | Fail | Fail or warn | Precision (fail or warn) |
|---|---|---|---|---|
| as generated | DOC_ARITHMETIC | 4/4 | 4/5 | 80.0% |
| as generated | DOC_UNKNOWN_VENDOR | 0/0 | 0/1 | 0.0% |
| as generated | INV_DELIVERED_QUANTITY | 8/8 | 8/9 | 88.9% |
| as generated | INV_DUPLICATE | 4/4 | 4/4 | 100.0% |
| as generated | INV_MISSING_PO | 4/4 | 4/4 | 100.0% |
| as generated | INV_PO_QUANTITY | 4/4 | 4/5 | 80.0% |
| as generated | INV_PO_TAX_RATE | 4/4 | 4/4 | 100.0% |
| as generated | INV_PO_UNIT_PRICE | 4/4 | 4/4 | 100.0% |
| as generated | INV_PO_VENDOR | 4/4 | 4/5 | 80.0% |
| all scanned | DN_LINE_NOT_ORDERED | 0/0 | 0/5 | 0.0% |
| all scanned | DN_PO_QUANTITY | 0/1 | 0/3 | 0.0% |
| all scanned | DN_PO_VENDOR | 0/0 | 0/4 | 0.0% |
| all scanned | DOC_ARITHMETIC | 4/8 | 4/10 | 40.0% |
| all scanned | DOC_MANDATORY_FIELDS | 0/1 | 0/1 | 0.0% |
| all scanned | DOC_UNKNOWN_VENDOR | 0/0 | 0/5 | 0.0% |
| all scanned | INV_DELIVERED_QUANTITY | 3/4 | 8/16 | 50.0% |
| all scanned | INV_DUPLICATE | 4/4 | 4/4 | 100.0% |
| all scanned | INV_LINE_NOT_ORDERED | 0/0 | 0/4 | 0.0% |
| all scanned | INV_MISSING_PO | 4/4 | 4/4 | 100.0% |
| all scanned | INV_PO_QUANTITY | 4/4 | 4/7 | 57.1% |
| all scanned | INV_PO_TAX_RATE | 2/2 | 3/3 | 100.0% |
| all scanned | INV_PO_UNIT_PRICE | 3/3 | 3/4 | 75.0% |
| all scanned | INV_PO_VENDOR | 4/4 | 4/5 | 80.0% |

## Duplicate invoices

| Input | Resent invoices | P / R (strong) | P / R (strong or possible) | Wrong |
|---|---|---|---|---|
| as generated | 4 | 100.0% / 100.0% | 100.0% / 100.0% | none |
| all scanned | 4 | 100.0% / 100.0% | 100.0% / 100.0% | none |

## Alarms without a planted defect

| Input | Document | Rule | Outcome | Message |
|---|---|---|---|---|
| as generated | B0047-INV | INV_PO_VENDOR | WARN | Could not be verified: Vendor differs (invoice Germany, purchase order Altamira Components GmbH), but a value was read with low confidence. |
| as generated | B0047-INV | DOC_UNKNOWN_VENDOR | WARN | Vendor 'Germany' is not in the vendor master. |
| as generated | B0048-INV | INV_PO_QUANTITY | WARN | Could not be verified: Quantity of TNR-K310 (invoice vs ordered): invoice 9 poe, purchase order 12 poe (difference -3); a value was read with low confidence, so this may be a misread. |
| as generated | B0048-INV | INV_DELIVERED_QUANTITY | WARN | Could not be verified: Quantity of TNR-K310 (invoiced vs delivered): invoice 9 poe, delivery note 12 poe (difference -3); a value was read with low confidence, so this may be a misread. |
| as generated | B0048-INV | DOC_ARITHMETIC | WARN | Could not be verified (a value involved was read with low confidence): line 1: quantity x unit price = amount: expected 634.86, printed 846.48. |
| all scanned | B0001-DN | DN_LINE_NOT_ORDERED | WARN | Could not be verified: 1GLV-CUT5 is on the delivery note but no line items could be read on the purchase order; ZNTRQ-2050 is on the delivery note but no line items could be read on the purchase order. |
| all scanned | B0001-INV | INV_LINE_NOT_ORDERED | WARN | Could not be verified: GLV-CUT5 is on the invoice but no line items could be read on the purchase order; TRQ-2050 is on the invoice but no line items could be read on the purchase order. |
| all scanned | B0005-INV | DOC_ARITHMETIC | FAIL | Amounts do not add up: sum of line amounts = subtotal: expected 701.88, printed 764.67. |
| all scanned | B0006-INV | DOC_ARITHMETIC | FAIL | Amounts do not add up: sum of line amounts = subtotal: expected 2086.75, printed 2147.45. |
| all scanned | B0008-DN | DN_LINE_NOT_ORDERED | WARN | Could not be verified: PLT-EUR is on the delivery note but not on the purchase order (an item code was read with low confidence, so the lines may belong together). |
| all scanned | B0008-INV | INV_LINE_NOT_ORDERED | WARN | Could not be verified: PLT-EUR is on the invoice but not on the purchase order (an item code was read with low confidence, so the lines may belong together). |
| all scanned | B0012-DN | DN_LINE_NOT_ORDERED | WARN | Could not be verified: die er is on the delivery note but not on the purchase order (an item code was read with low confidence, so the lines may belong together). |
| all scanned | B0013-DN | DOC_UNKNOWN_VENDOR | WARN | Vendor 'DN-SPT-969648' is not in the vendor master. |
| all scanned | B0013-DN | DN_PO_QUANTITY | WARN | Could not be verified: Quantity of GLV-CUT5 (delivery note vs ordered): delivery note 2 pair, purchase order 4 pair (difference -2); a value was read with low confidence, so this may be a misread. |
| all scanned | B0013-DN | DN_PO_VENDOR | WARN | Could not be verified: Vendor differs (delivery note DN-SPT-969648, purchase order Sundaram Precision Tools Pvt. Ltd.), but a value was read with low confidence. |
| all scanned | B0014-DN | DOC_UNKNOWN_VENDOR | WARN | Vendor 'DN-ACG-765412' is not in the vendor master. |
| all scanned | B0014-DN | DN_PO_VENDOR | WARN | Could not be verified: Vendor differs (delivery note DN-ACG-765412, purchase order Altamira Components GmbH), but a value was read with low confidence. |
| all scanned | B0016-DN | DOC_UNKNOWN_VENDOR | WARN | Vendor 'DN-BOS-450484' is not in the vendor master. |
| all scanned | B0016-DN | DN_PO_QUANTITY | WARN | Could not be verified: Quantity of PAP-A4-80 (delivery note vs ordered): delivery note 12 ream, purchase order 20 ream (difference -8); a value was read with low confidence, so this may be a misread. |
| all scanned | B0016-DN | DN_PO_VENDOR | WARN | Could not be verified: Vendor differs (delivery note DN-BOS-450484, purchase order Bluepeak Office Solutions LLC), but a value was read with low confidence. |
| all scanned | B0021-PO | DOC_MANDATORY_FIELDS | FAIL | Mandatory fields missing: po_date. |
| all scanned | B0021-DN | DN_LINE_NOT_ORDERED | WARN | Could not be verified: GLV-CUT5 is on the delivery note but no line items could be read on the purchase order; CAL-150D is on the delivery note but no line items could be read on the purchase order. |
| all scanned | B0021-INV | INV_LINE_NOT_ORDERED | WARN | Could not be verified: GLV-CUT5 is on the invoice but no line items could be read on the purchase order; CAL-150D is on the invoice but no line items could be read on the purchase order. |
| all scanned | B0023-INV | INV_PO_UNIT_PRICE | WARN | Could not be verified: Unit price of BRG-6204: invoice 21.00 USD, purchase order 5.21 USD (difference +15.79); a value was read with low confidence, so this may be a misread. |
| all scanned | B0023-INV | INV_DELIVERED_QUANTITY | WARN | Could not be verified: BRG-6204 is invoiced but on no delivery note (an item code was read with low confidence, so the lines may belong together). |
| all scanned | B0031-INV | INV_DELIVERED_QUANTITY | WARN | Could not be verified: VLV-BLO50 is invoiced but no line items could be read on the delivery note; GSK-150A is invoiced but no line items could be read on the delivery note; HYD-HS12 is invoiced but no line items could be read on the delivery note (and 1 more). |
| all scanned | B0032-DN | DOC_UNKNOWN_VENDOR | WARN | Vendor 'DN-BOS-856477' is not in the vendor master. |
| all scanned | B0032-DN | DN_PO_VENDOR | WARN | Could not be verified: Vendor differs (delivery note DN-BOS-856477, purchase order Bluepeak Office Solutions LLC), but a value was read with low confidence. |
| all scanned | B0037-INV | INV_PO_QUANTITY | WARN | Could not be verified: Quantity of CHR-ERG2 (invoice vs ordered): invoice 7 pcs, purchase order 2 pcs (difference +5); a value was read with low confidence, so this may be a misread. |
| all scanned | B0037-INV | INV_DELIVERED_QUANTITY | WARN | Could not be verified: Quantity of CHR-ERG2 (invoiced vs delivered): invoice 7 pcs, delivery note 2 pcs (difference +5); a value was read with low confidence, so this may be a misread. |

## Notes

* Every bundle of an input is matched in one department: the invoice must find its own order and delivery note by reference, and duplicate alarms can come from any bundle. Documents are matched with all others on file (the state after the last upload, whatever the arrival order).
* FAIL is a confirmed discrepancy; WARN means a difference involves a value read with low confidence (or the referenced order is not on file). Both send the document to the review queue; the columns show which of the two raised the alarm.
* A defect counts as detected when one of its rules raised it. Pair precision / recall count (document, rule) pairs, so a quantity billed above the order needs both quantity rules.
* 'Defect-free in extraction review' is the extraction's own routing (uncertain or missing values), independent of the rules; the review queue takes both.
* Alarms without a planted defect: 46 in total, the first 30 listed; the JSON report has all of them.
* Byte-identical re-uploads are caught by the file hash at upload (exact, not measured here); this suite measures resent invoices with a different layout and date format.

## Provenance

```json
{
  "dataset": {
    "name": "synthetic-core",
    "generator_version": "1.0",
    "seed": 53,
    "bundles_per_scenario": 4,
    "scenarios": [
      "CLEAN_MATCH",
      "DUPLICATE_INVOICE",
      "LONG_MULTIPAGE",
      "MISSING_PO_REFERENCE",
      "QUANTITY_MISMATCH",
      "SCANNED_DOCUMENTS",
      "SHORT_DELIVERY",
      "TAX_RATE_MISMATCH",
      "TOTAL_ARITHMETIC_ERROR",
      "UNIT_PRICE_MISMATCH",
      "VENDOR_MISMATCH",
      "VENDOR_NAME_VARIANT"
    ],
    "documents_per_input": 148,
    "quick": false
  },
  "config": {
    "rules": "default rule set (19 rules, default parameters)",
    "comparison_min_confidence": 0.85,
    "reference_date": "2026-10-01",
    "llm_mode": "never",
    "expected_rules": {
      "UNIT_PRICE_MISMATCH": [
        "INV_PO_UNIT_PRICE"
      ],
      "QUANTITY_MISMATCH": [
        "INV_DELIVERED_QUANTITY",
        "INV_PO_QUANTITY"
      ],
      "BILLED_QUANTITY_EXCEEDS_DELIVERED": [
        "INV_DELIVERED_QUANTITY"
      ],
      "MISSING_PO_REFERENCE": [
        "INV_MISSING_PO"
      ],
      "TOTAL_MISMATCH": [
        "DOC_ARITHMETIC"
      ],
      "TAX_RATE_MISMATCH": [
        "INV_PO_TAX_RATE"
      ],
      "VENDOR_MISMATCH": [
        "INV_PO_VENDOR"
      ],
      "DUPLICATE_INVOICE": [
        "INV_DUPLICATE"
      ]
    },
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
