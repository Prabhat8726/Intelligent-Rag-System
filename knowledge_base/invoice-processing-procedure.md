---
title: Invoice Processing Procedure
document_key: invoice-processing-procedure
version: 2026.2
category: PROCEDURE
effective_from: 2026-03-01
sensitivity: INTERNAL
owner: Accounts Payable
---

# Invoice Processing Procedure

SYNTHETIC DOCUMENT - written for the demo knowledge base of Meridian Manufacturing Co.; not a real company procedure.

## 1. Receiving invoices

Vendors send invoices to the Accounts Payable mailbox or upload them to the vendor portal. Paper invoices are scanned on the day they arrive. Every invoice is uploaded to the document intelligence platform, which reads it, matches it with its purchase order and delivery notes, and runs the business rules.

## 2. Registration

An invoice is registered within one business day of receipt. Registration checks that the invoice is addressed to a Meridian legal entity, shows the vendor's tax identification number, has an invoice number and date, and states the currency, the tax amount and the total.

## 3. Matching and exceptions

Invoices that pass the three-way match are posted automatically. Invoices sent to the review queue are handled as follows.

### 3.1 Missing purchase order

Ask the requester for the purchase order number. If no purchase order exists, the requester raises one; it must be approved before the invoice is posted. If the purchase order is not provided within five business days, return the invoice to the vendor.

### 3.2 Price or quantity difference

Check the purchase order and the delivery notes. For a price difference without an approved change order, ask the vendor for a credit note or a corrected invoice. For billed quantities above the delivered quantity, hold the invoice until the delivery is complete or a credit note arrives.

### 3.3 Tax rate difference

Compare the tax rate with the purchase order and the applicable tax rules. If the invoice applies a wrong rate, request a corrected invoice; never correct tax amounts manually.

### 3.4 Possible duplicate

Contact the vendor to confirm whether the invoice was sent twice. Reject confirmed duplicates in the platform with the reason, so the decision is recorded.

### 3.5 Values the system could not read

When the platform marks a value as uncertain, compare it with the original document and correct the value in the platform. Corrections are kept with the name of the person who made them.

## 4. Posting and payment

Approved invoices are posted to the ledger on the day of approval. Payments are made in the weekly payment run every Thursday. Invoices due before the next run are paid in the current run. Early-payment discounts are taken as described in the Procurement Policy.

## 5. Month-end

At month-end, Accounts Payable lists all invoices held for more than ten business days and sends the list to the Finance Manager.
