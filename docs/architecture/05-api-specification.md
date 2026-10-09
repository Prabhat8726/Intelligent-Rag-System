# 05 — API Specification

The authoritative, always-current contract is the generated OpenAPI document
(`GET /openapi.json`, Swagger UI at `/docs` when `API_DOCS_ENABLED=true`). This
page defines conventions and the full planned surface, marking what exists now.

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | Business endpoints under `/api/v1`. Infrastructure endpoints (`/health`, `/health/ready`, `/metrics`) are unversioned. |
| Auth | `Authorization: Bearer <JWT>` (HS256, `iss`/`aud`/`exp`/`iat`/`jti`/`sub`/`role` claims). MCP/automation use per-user API tokens (Phase 7) with the same permission checks. |
| Authorization | Permission checks server-side on every endpoint (`require_permission`). Resource-level checks via the access-policy module. Resources the caller cannot see return **404**, not 403, to prevent ID enumeration. |
| Content type | `application/json`; uploads `multipart/form-data`. Request models reject unknown fields (`extra="forbid"`). |
| Errors | RFC 9457 `application/problem+json`: `type`, `title`, `status`, `detail`, `instance`, `request_id`, optional `errors[]` (field, message). No stack traces, no internal identifiers beyond `request_id`. |
| Request IDs | `X-Request-ID` accepted (validated, ≤ 64 safe chars) or generated; echoed on every response and bound into logs + audit. |
| Pagination | `?limit=` (1–100, default 25) `&offset=`; responses `{items, total, limit, offset}`. Audit logs use keyset `?before_id=`. |
| Async work | Long operations return **202** with a resource id + `status`; clients poll the resource (`GET /analysis/{id}`). |
| Timestamps | ISO-8601 UTC. |
| Idempotency | Duplicate uploads are detected by SHA-256; HITL decisions are idempotent per action state. |

## 2. Endpoint surface

Legend: ✅ implemented (phase in which it shipped) · 🔜 planned (phase number)

### Infrastructure
| Method | Path | Auth | Description | Status |
|---|---|---|---|---|
| GET | `/health` | none | Liveness: process is up. `{"status":"ok"}` | ✅ |
| GET | `/health/ready` | none | Readiness: DB reachable, migrations at head. 503 + per-check status otherwise | ✅ |
| GET | `/metrics` | bearer `METRICS_TOKEN` | Prometheus exposition | 🔜 11 |

### Auth & users
| Method | Path | Permission | Description | Status |
|---|---|---|---|---|
| POST | `/api/v1/auth/login` | none | `{email, password}` → `{access_token, token_type, expires_in, user}`. Lockout after N failures; uniform error for unknown user / wrong password / locked / inactive | ✅ |
| GET | `/api/v1/auth/me` | authenticated | Current user, role, department, effective permissions | ✅ |
| POST | `/api/v1/auth/refresh` · `/logout` | cookie | Refresh-token rotation + revocation | 🔜 9 |
| GET/POST/PATCH | `/api/v1/users` | `users:manage` | Admin user management | 🔜 8 |

### Documents (Modules 1–8, 27–29)
| Method | Path | Permission | Status |
|---|---|---|---|
| POST | `/api/v1/documents` (multipart: `file`, `sensitivity`, `department_id` admins only) → 201 | `documents:upload` | ✅ 2 |
| GET | `/api/v1/documents` (filters: `status`, `document_type`, `vendor_id`, `mine`, `q` filename, `created_from`/`created_to`; items carry the matched `vendor`) | `documents:read` + scope | ✅ 2 (vendor ✅ 4) |
| GET | `/api/v1/documents/{id}` (detail: inspection, latest job; from Phase 3 also pages, current classification + history, review reasons, sensitivity assessment) | `documents:read` + scope | ✅ 2/3 |
| DELETE | `/api/v1/documents/{id}` (soft delete, cancels queued jobs) → 204 | `documents:delete` + scope | ✅ 2 |
| GET | `/api/v1/documents/{id}/file` (attachment, `nosniff`, sandbox CSP) | `documents:read` + scope | ✅ 2 |
| POST | `/api/v1/documents/{id}/process` (re-process) → 202, 409 if already active | `documents:process` + scope | ✅ 2 |
| GET | `/api/v1/documents/{id}/pages/{n}` (text, words with boxes `[text,x0,y0,x1,y1,conf,size]`, layout) | `documents:read` + scope | ✅ 3 |
| GET | `/api/v1/documents/{id}/pages/{n}/image` (PNG preview, `nosniff`, sandbox CSP, `private` cache) | `documents:read` + scope | ✅ 3 |
| GET | `/api/v1/documents/{id}/tables` (stitched tables with rows) | `documents:read` + scope | ✅ 3 |
| PATCH | `/api/v1/documents/{id}/classification` `{document_type, note?}` (human correction, audited) | `documents:review` + scope | ✅ 3 |
| GET | `/api/v1/documents/{id}/extraction` (current extraction: schema, method, provider/model, review level, overall confidence, consistency checks, signals, matched vendor, every field with original and normalized value, page, quote, box, evidence status, origin, confidence and its signals, competing reading, correction) → 404 if the type has no schema or nothing was extracted | `documents:read` + scope | ✅ 4 |
| GET | `/api/v1/documents/{id}/evidence?field_path=` (where each value was read: page, quote, box, evidence status) | `documents:read` + scope | ✅ 4 |
| PATCH | `/api/v1/documents/{id}/extraction/fields/{field_id}` `{value, note?}` (value as printed; empty = not on the document; normalized and re-scored; review reasons and status recomputed; audited without values) → 422 if the value does not fit the field type | `documents:review` + scope | ✅ 4 |
| GET | `/api/v1/documents/{id}/findings` (comparisons the caller can see, rule results with outcome and message, duplicates in both directions, the open review task, review history) | `documents:read` + scope | ✅ 5 |
| GET | `/api/v1/documents/{id}/versions` (newest first: processed, current) | `documents:read` + scope | ✅ 5 |
| POST | `/api/v1/documents/{id}/versions` (multipart `file`; becomes the current version and is processed; same validation and limits as an upload) → 201; 409 while processing or when the file is identical to the current version | `documents:upload` + scope | ✅ 5 |
| GET | `/api/v1/documents/{id}/versions/compare?from=&to=` (clauses ADDED / REMOVED / MODIFIED / UNCHANGED with word-level changes and renumbering) → 409 if a version is not processed | `documents:read` + scope | ✅ 5 |
| GET | `/api/v1/documents/{id}/duplicates` | `documents:read` | 🔜 5 |

Upload rules (Phase 2, `docintel/documents/validation.py`):

| Check | Result |
|---|---|
| Body larger than `UPLOAD_MAX_BYTES` (+1 MiB multipart overhead), by `Content-Length` or while streaming | 413 before the handler runs |
| File larger than `UPLOAD_MAX_BYTES` while spooling | 413 |
| Extension, declared MIME type and magic bytes disagree; unsupported extension or content | 415 |
| Empty file; corrupt or user-password-protected PDF; undecodable image; more than `UPLOAD_MAX_PAGES` pages; more than `UPLOAD_MAX_IMAGE_PIXELS` pixels; unknown `department_id` | 422 |
| `department_id` sent by a non-admin | 403 |
| Same SHA-256 as a document the uploader can see | 201, accepted and flagged with `duplicate_of_id` |

The stored file, document, version, processing job and audit row are created
in one transaction; if the transaction fails the stored file is deleted.
Filenames are normalized (NFKC, path, control and bidi characters removed) and
are display data only: storage keys are `documents/{id}/v{n}/original.{ext}`.

### Vendors (Module 8 normalization target)
| Method | Path | Permission | Status |
|---|---|---|---|
| GET | `/api/v1/vendors?q=&limit=&offset=` (`q`: fuzzy name or exact tax ID) | `documents:read` | ✅ 4 |
| GET | `/api/v1/vendors/{id}` | `documents:read` | ✅ 4 |
| POST | `/api/v1/vendors` `{canonical_name, aliases?, tax_id?, default_currency?, payment_terms_days?, is_active?}` → 201, 409 duplicate name (audited) | `vendors:manage` | ✅ 4 |
| PATCH | `/api/v1/vendors/{id}` (partial update, e.g. add a confirmed alias; audited) | `vendors:manage` | ✅ 4 |

Vendor master data is shared across departments: it holds supplier names and terms, not
document content.

### Comparison, rules, review
| Method | Path | Permission | Status |
|---|---|---|---|
| POST | `/api/v1/comparisons` `{documents:[{document_id, role}]}` (2–10: an invoice with a purchase order and/or delivery notes, or a delivery note with a purchase order; every document visible to the caller and processed) → 201, stored with origin `MANUAL`; no rules run, no review task | `comparisons:create` + scope | ✅ 5 |
| GET | `/api/v1/comparisons` (filters: `document_id`, `comparison_type`, `origin`, `with_issues`; only comparisons whose documents are all visible) · `/comparisons/{id}` (every item with explanation, tolerance and both sides' evidence) | `documents:read` + scope | ✅ 5 |
| GET | `/api/v1/rules` · `/rules/{code}` (with the JSON schema of its parameters) | `rules:read` | ✅ 5 |
| PATCH | `/api/v1/rules/{code}` `{params?, severity?, is_enabled?, note?}` (parameters validated by the rule type; version +1; audited with before/after) → 422 on invalid parameters | `rules:manage` | ✅ 5 |
| POST | `/api/v1/rules/evaluate` `{document_ids}` (1–100: re-run matching and rules for each document and its related documents, e.g. after a rule change) | `documents:process` + scope | ✅ 5 |
| GET | `/api/v1/review-tasks` (filters: `state` open/closed/all, `task_type`, `priority`, `assigned` any/me/unassigned, `document_id`, `overdue`; most urgent first; caller's scope) · `/review-tasks/{id}` | `reviews:work` + scope | ✅ 5 |
| POST | `/api/v1/review-tasks/{id}/claim` · `/release` (another user's claim only by managers and admins → 409 otherwise) | `reviews:work` + scope | ✅ 5 |
| POST | `/api/v1/review-tasks/{id}/resolve` `{resolution: APPROVED \| CORRECTED \| REJECTED, note?}` (note required to reject; the document leaves review; audited) | `reviews:work` + scope | ✅ 5 |

### Knowledge, search, RAG
| Method | Path | Permission | Status |
|---|---|---|---|
| POST/GET/DELETE | `/api/v1/knowledge/documents[/{id}]` | `knowledge:manage` / `knowledge:read` | 🔜 6 |
| POST | `/api/v1/knowledge/search` (retrieval only, scored chunks) | `knowledge:read` | 🔜 6 |
| POST | `/api/v1/knowledge/query` (answer + citations) | `knowledge:read` | 🔜 6 |
| POST | `/api/v1/search` (semantic + metadata over business documents) | `documents:read` | 🔜 6 |

### Agent analysis
| Method | Path | Permission | Status |
|---|---|---|---|
| POST | `/api/v1/analysis` `{query, document_ids?}` → 202 | `analysis:run` | 🔜 7 |
| GET | `/api/v1/analysis` · `/analysis/{id}` (status, findings, tool-call summary, recommendation) | `analysis:read` | 🔜 7 |

### Workflows & HITL
| Method | Path | Permission | Status |
|---|---|---|---|
| POST | `/api/v1/workflows` `{workflow_type, document_id}` | `workflows:start` | 🔜 8 |
| GET | `/api/v1/workflows` · `/workflows/{id}` | `workflows:read` | 🔜 8 |
| POST | `/api/v1/workflows/{id}/approve` `{reason}` | `workflows:approve` + required_role + maker-checker | 🔜 8 |
| POST | `/api/v1/workflows/{id}/reject` `{reason}` (reason mandatory) | `workflows:approve` | 🔜 8 |
| POST/GET | `/api/v1/reports` · `/reports/{id}` · `/reports/{id}/download` | `reports:create` / `reports:read` | 🔜 8 |

### Audit, dashboard, evaluation
| Method | Path | Permission | Status |
|---|---|---|---|
| GET | `/api/v1/audit-logs` (filters: actor, action, entity, date) | `audit:read` (ADMIN all; MANAGER own department) | 🔜 8 |
| GET | `/api/v1/dashboard/summary` | `dashboard:read` | 🔜 9 |
| GET | `/api/v1/evaluations` · `/evaluations/{id}` | `evaluations:read` | 🔜 10 |

## 3. Role → permission matrix

Defined in code (`docintel/auth/permissions.py`), covered by tests.

| Permission | ADMIN | MANAGER | ANALYST | REVIEWER | VIEWER |
|---|:-:|:-:|:-:|:-:|:-:|
| `documents:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `documents:upload` | ✓ | ✓ | ✓ | | |
| `documents:process` | ✓ | ✓ | ✓ | | |
| `documents:review` (corrections) | ✓ | ✓ | ✓ | ✓ | |
| `documents:delete` | ✓ | ✓ | | | |
| `vendors:manage` | ✓ | ✓ | | | |
| `comparisons:create` | ✓ | ✓ | ✓ | ✓ | |
| `rules:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `rules:manage` | ✓ | | | | |
| `reviews:work` | ✓ | ✓ | ✓ | ✓ | |
| `knowledge:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `knowledge:manage` | ✓ | ✓ | | | |
| `analysis:run` | ✓ | ✓ | ✓ | ✓ | |
| `analysis:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `workflows:start` | ✓ | ✓ | ✓ | | |
| `workflows:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `workflows:approve` | ✓ | ✓ | | ✓ | |
| `reports:create` | ✓ | ✓ | ✓ | ✓ | |
| `reports:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `audit:read` | ✓ | ✓ | | | |
| `dashboard:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `evaluations:read` | ✓ | ✓ | ✓ | | |
| `users:manage` | ✓ | | | | |

`workflows:approve` is necessary but not sufficient: each action also carries a
`required_role` (e.g. `HIGH` risk → `MANAGER`), and the proposer can never
approve their own action.

## 4. Example error

```http
HTTP/1.1 401 Unauthorized
Content-Type: application/problem+json
X-Request-ID: 3f2a0c9e8b7d4e1f

{
  "type": "about:blank",
  "title": "Unauthorized",
  "status": 401,
  "detail": "Invalid email or password.",
  "instance": "/api/v1/auth/login",
  "request_id": "3f2a0c9e8b7d4e1f"
}
```
