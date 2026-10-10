# Production readiness checklist

The master prompt's §52 checklist, item by item, with the evidence for each answer. Status on
2026-10-10, at the end of Phase 11.

Legend: ✅ verified by the evidence named · ⚠️ works with a stated limit · ❌ not done.

"Tests" are the backend suite (904 tests: unit, integration against real PostgreSQL, security),
the frontend suite (78 tests), the browser test of the demonstration path (`make e2e`), the
checked demonstration through the API (`make demo`) and the smoke test of the Docker stack
(`make smoke`). Numbers come from the committed reports in `evaluation/reports/` (the README's
table is generated from them) and `evaluation/load/`. All data is synthetic.

| Item | Status | Evidence |
|---|---|---|
| Backend works | ✅ | 904 backend tests; smoke test through nginx on a fresh hardened stack with two API replicas; `make demo` (17 checked steps) on a fresh stack and on the upgraded long-lived stack |
| Frontend works | ✅ | 78 component tests, ESLint, `tsc`, production build; `make e2e` (Playwright) of the demo path passed on the fresh stack — one of seven runs failed in the browser without a server-side error and was not reproduced (see the Phase 11 report) |
| Database migrations work | ✅ | Upgrade/downgrade/upgrade and model–migration drift tests (`test_database.py`); migration 0011 applied to a fresh database and to the long-lived one; a pre-deploy dump restored into a scratch database matched the source |
| Authentication works | ✅ | `test_auth_api.py`, `test_sessions.py`, `test_tokens.py`, `test_passwords.py`; lockout, uniform errors, refresh rotation and reuse detection |
| Authorization works | ✅ | `test_permissions.py`, the `tests/security/` suite (cross-department, privilege escalation, maker-checker), workflow evaluation: 0 bypasses in 152 attempts |
| Document upload works | ✅ | `test_documents_api.py`, smoke test, `make process` (37 documents through the API) |
| File validation works | ✅ | `test_upload_validation.py`, `test_body_limit_middleware.py`, `test_document_security.py` (spoofed types, oversized, encrypted, pixel bombs) |
| Storage works | ✅ | `test_storage.py` against the local backend and S3 (moto server), including the listing used by reconciliation; `test_retention.py` |
| OCR works | ✅ | `test_ocr.py`; OCR report: CER 1.2% clean, 2.3% light scan, 10.4% heavy scan |
| PDF processing works | ✅ | `test_native_text.py`, `test_inspection.py`, `test_ingestion_e2e.py`; mixed PDFs handled per page |
| Image processing works | ✅ | PNG, JPEG and TIFF uploads accepted and inspected (`test_documents_api.py`, `test_inspection.py`), OCR'd and processed (`test_understanding.py`, `test_worker.py`); the dataset's image documents (e.g. TIFF delivery notes) processed by `make process` on the stack |
| Classification works | ✅ | `test_classification.py`; classification report: 100% accuracy on 90 rendered documents; the model fallback is not measured (needs a model) |
| Table extraction works | ⚠️ | `test_tables.py`; tables report: native 100% rows exact, scanned 150 DPI row recall 0.894 — scanned tables are the weakest area |
| Structured extraction works | ✅ | `test_extraction.py`, `test_fields_*.py`, `test_extraction_api.py`; field F1 1.000 native, 0.996 scanned (layout extractor); model-assisted extraction not measured |
| Schema validation works | ✅ | Versioned schemas, malformed and invalid model output handled (`test_extraction.py`, `test_ai_providers_phase4.py`) |
| Field provenance works | ✅ | Every value carries page, box and quote or is flagged (`test_fields_service.py`, `/evidence` API tests) |
| Normalization works | ✅ | `test_fields_normalize.py`, `test_vendor_matching.py` |
| Comparison works | ✅ | `test_matching.py`, `test_matching_api.py`; discrepancy report: 100% / 100% on native documents |
| Business rules work | ✅ | `test_rules.py`; 19 rules; discrepancy and workflow reports |
| Discrepancy detection works | ⚠️ | 100% precision/recall as generated; on 150-DPI scans 80.0% / 77.8% for confirmed failures (OCR misreads) |
| Review queue works | ✅ | `test_matching_api.py`, `test_review_items.py`, frontend review tests; exercised by `make demo` and `make e2e` |
| Knowledge base works | ✅ | `test_knowledge_api.py`, `test_knowledge_ingest_e2e.py`, `make seed-knowledge` on the stack |
| RAG works | ⚠️ | Retrieval: hit@5 100%, MRR 0.938 (tuning) / 0.950 (holdout) with offline embeddings; evidence gate and access filters tested; **generated answers with a real model are not measured** (no model in the build environment; covered by tests with scripted models) |
| Citations work | ⚠️ | Citation validation and grounding checks tested with scripted models (`test_rag.py`, `test_knowledge_rag.py`); not measured with a real model |
| Agent workflow works | ⚠️ | Agent report (deterministic mode): 100% task success on 70 development and 60 held-out runs, 0 unsafe recommendations; with a model: not measured |
| Agent tools work | ✅ | `test_agent_tools.py` (10 tools, permissions, argument abuse); tool selection precision 99.2%, recall 100% |
| MCP works where implemented | ✅ | `test_mcp.py` (stdio and streamable HTTP, scoped hashed tokens, host checks) |
| Human approval works | ✅ | `test_workflows.py`, workflow report (maker-checker), `make demo` step "analyst refused, reviewer approves", `make e2e` |
| Workflow actions work | ✅ | `test_workflows.py`; workflow report: 100% expected proposals, 284 of 284 state changes audited |
| Audit logs work | ✅ | Append-only at the database level (trigger; `test_database.py`), audit API tests; purge events (`test_retention.py`); the client address is the real one (smoke test, rehearsal) |
| Search works | ✅ | `test_document_search.py`, `test_search_query.py`; search report: 100% / 100% on structured questions |
| Duplicate detection works | ✅ | Discrepancy report: 4 of 4 resent invoices found, 0 wrong pairs, also on scans |
| Reports work | ✅ | `test_reports.py`; workflow report: 100% re-render to the stored hash |
| Synthetic dataset works | ✅ | `test_synthetic.py`; used by every suite, `make process`, the load test |
| Evaluation framework works | ✅ | 11 suites, 56 gates; the quick run passed every gate on the final code; README table generated from the reports and checked by a test |
| Unit tests exist | ✅ | `backend/tests/unit/` (40 files), frontend component tests |
| Integration tests exist | ✅ | `backend/tests/integration/` (29 files) against real PostgreSQL |
| End-to-end tests exist | ✅ | `make e2e` (Playwright), `make demo`, `scripts/smoke_test.sh`, in-process ingestion and demo tests |
| Security tests exist | ✅ | `backend/tests/security/` (7 files), rate-limit, metrics and retention tests, gitleaks, `pip-audit`, `npm audit` |
| Docker works | ✅ | Hardened compose stack (read-only, no capabilities, limits) built and run fresh; production layout rehearsed with TLS, replicas, backups and rollback |
| CI works | ⚠️ | Workflows written and lint-clean (actionlint), and every step's commands were run locally; **the GitHub workflows have never run** (0 runs: CI triggers on `main` and pull requests, and work so far is on a feature branch) |
| Observability works | ✅ | Metrics on API and worker, token-protected; Prometheus scraped both replicas and the worker; 13 alert rules with passing unit tests; a stopped worker fired `DocintelWorkerDown`; Grafana dashboard provisioned ([monitoring](monitoring.md)) |
| Deployment configuration exists | ✅ | `deploy/` (compose override, Caddy, backups, deploy script), release and deploy workflows ([deployment](deployment.md)); **no deployment has been performed** |
| Documentation is complete | ✅ | Architecture (13 documents, 80 ADRs), configuration and local setup, operations (deployment, monitoring, runbooks, load testing, this checklist), [project overview](../project-overview.md) |
| Demo is reproducible | ✅ | `make demo` (random seed each run) and `make e2e` on fresh and upgraded stacks |
| No secrets are committed | ✅ | gitleaks over the full history: no leaks; `.env` is git-ignored (the only working-tree finding is that local file); placeholders only in examples |
| No fabricated metrics are presented | ✅ | Every number is generated from or copied from a committed report; unmeasured items say "Not yet measured" with the reason |
| All claimed features actually work | ⚠️ | Everything claimed as working has a test or a measurement above; claims with limits are marked ⚠️ here and in the README; model-assisted paths and a real deployment are explicitly not claimed |

## What blocks a production go-live

1. A host, a domain and credentials to run the deploy workflow (none exist in this
   environment), followed by the release workflow's first run (image scan and push) and a
   staging deployment with a load generator on its own machine.
2. A decision on the model provider for real documents (paid tier or a self-hosted model) and
   measurements of the model-assisted paths with it.
3. Recalibration of the confidence thresholds on labelled real documents.
4. An alert receiver (Alertmanager) and separate database roles for migrations and runtime.
