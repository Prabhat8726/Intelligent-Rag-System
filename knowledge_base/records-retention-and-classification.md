---
title: Records Retention and Information Classification
document_key: records-retention
version: 2026.1
category: COMPLIANCE
effective_from: 2026-01-01
sensitivity: INTERNAL
owner: Compliance
---

# Records Retention and Information Classification

SYNTHETIC DOCUMENT - written for the demo knowledge base of Meridian Manufacturing Co.; not a real company policy.

## 1. Information classification

Every document is labelled with one of four classification levels when it is stored.

| Level | Meaning | Examples |
| --- | --- | --- |
| PUBLIC | May be shared outside the company | Published price lists, press releases |
| INTERNAL | For employees and contractors | Policies, purchase orders, invoices |
| CONFIDENTIAL | Limited to the people who need it | Contracts, negotiation positions, personnel files |
| RESTRICTED | Strictly limited; legal or regulatory protection | Bank statements, payment card data, identity numbers |

Confidential and restricted content may not be sent to external AI services. The document intelligence platform enforces this automatically and raises the level when it detects payment card or identity numbers.

## 2. Retention periods

| Record | Retention |
| --- | --- |
| Invoices, credit notes, receipts | 10 years after the end of the fiscal year |
| Purchase orders and delivery notes | 7 years after the end of the fiscal year |
| Contracts | 10 years after expiry or termination |
| Expense claims | 7 years |
| Resumes of candidates not hired | 6 months after the position is filled |
| Audit logs of the document platform | 7 years |

Records under a legal hold are kept until Legal releases the hold, even after the retention period.

## 3. Deletion

At the end of the retention period, records are deleted in the systems of record and in backups within 90 days. Deleted documents disappear from search immediately; their audit trail is kept for the retention period of the audit log.
