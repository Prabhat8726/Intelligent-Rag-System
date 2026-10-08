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

Legend: ✅ implemented (Phase 0) · 🔜 planned (phase number)

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
| POST | `/api/v1/documents` | `documents:upload` | 🔜 2 |
| GET | `/api/v1/documents` (filters: status, type, owner, vendor, date range, q) | `documents:read` | 🔜 2 |
| GET | `/api/v1/documents/{id}` | `documents:read` + scope | 🔜 2 |
| DELETE | `/api/v1/documents/{id}` (soft delete) | `documents:delete` | 🔜 2 |
| GET | `/api/v1/documents/{id}/file` (attachment, `nosniff`) | `documents:read` | 🔜 2 |
| POST | `/api/v1/documents/{id}/process` (re-process) | `documents:process` | 🔜 2 |
| GET | `/api/v1/documents/{id}/pages/{n}` · `/pages/{n}/image` | `documents:read` | 🔜 3 |
| PATCH | `/api/v1/documents/{id}/classification` (human correction) | `documents:review` | 🔜 3 |
| GET | `/api/v1/documents/{id}/extraction` | `documents:read` | 🔜 4 |
| GET | `/api/v1/documents/{id}/evidence` | `documents:read` | 🔜 4 |
| PATCH | `/api/v1/documents/{id}/fields/{field_id}` (correction) | `documents:review` | 🔜 4 |
| POST/GET | `/api/v1/documents/{id}/versions` · `/versions/compare?from=&to=` | `documents:upload` / `read` | 🔜 5 |
| GET | `/api/v1/documents/{id}/duplicates` | `documents:read` | 🔜 5 |

### Comparison, rules, review
| Method | Path | Permission | Status |
|---|---|---|---|
| POST | `/api/v1/comparisons` `{comparison_type, documents:[{id, role}]}` | `comparisons:create` | 🔜 5 |
| GET | `/api/v1/comparisons` · `/comparisons/{id}` | `documents:read` | 🔜 5 |
| GET | `/api/v1/rules` · PATCH `/rules/{id}` | `rules:read` / `rules:manage` | 🔜 5 |
| POST | `/api/v1/rules/evaluate` `{document_ids \| comparison_id}` | `comparisons:create` | 🔜 5 |
| GET | `/api/v1/review-tasks` · POST `/{id}/claim` · POST `/{id}/resolve` | `reviews:work` | 🔜 5 |

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
