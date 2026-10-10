# Monitoring

Metrics (Module 24), dashboards and alerts. Decisions: ADR-073 (metrics), ADR-079 (Prometheus,
Grafana, alert rules).

## Where the metrics come from

| Source | Endpoint | Contents |
|---|---|---|
| Each API replica | `GET /metrics` on port 8000 (not under `/api/`, not proxied by the web container, refused by Caddy at the edge) | HTTP requests and latency by route template, database statement timings, model calls made by the API (questions), rate-limit refusals, the Python process; then the **gauges read from the database** at scrape time |
| Each worker | `WORKER_METRICS_PORT` (compose: 9100), internal network only | Job attempts and duration by outcome, pipeline stage durations, OCR time per page, model calls, tokens and estimated cost, extraction confidence, investigation durations, tool calls, database statement timings |

With `METRICS_TOKEN` set (required in staging and production) both endpoints answer 401 unless
the scraper sends `Authorization: Bearer <token>`; the comparison is constant-time.

Counters and histograms are per process, so Prometheus scrapes every replica (it finds them by
DNS, `deploy/observability/prometheus.yml`) and queries sum over instances. The database gauges
describe shared state and are identical on every API replica: queries take `max` over
instances, never `sum`.

**Labels are closed sets.** Routes are templates (`/api/v1/documents/{document_id}`), unknown
HTTP methods are `other`, unmatched paths are `unmatched`, model purposes are reduced to
`classification | extraction | rag | agent | diagnostics | other`, everything else is an enum
value. No label carries a document id, a filename, a user or any content, so a client cannot
create time series and metrics hold no personal data.

## Metric catalogue

All names start with `docintel_`.

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `http_requests_total` | counter | method, route, status | Requests answered |
| `http_request_duration_seconds` | histogram | method, route | Time to the end of the response |
| `rate_limited_requests_total` | counter | scope (`login`, `upload`, `search`, `ai`) | Requests refused with 429 by the per-caller limits (ADR-074) |
| `db_statement_duration_seconds` | histogram | operation (`select`, `insert`, `update`, `delete`, `with`, `other`, `error`) | Statement execution time |
| `worker_jobs_total` | counter | job_type, outcome (`completed`, `retried`, `failed`, `skipped`, `lease_lost`, `error`) | Job attempts |
| `worker_job_duration_seconds` | histogram | job_type, outcome | Claim to end of an attempt |
| `pipeline_stage_duration_seconds` | histogram | stage (`integrity` … `index`) | Each document-processing stage of completed jobs |
| `ocr_page_duration_seconds` | histogram | outcome (`ok`, `error`) | Tesseract time per page image |
| `llm_calls_total` | counter | provider, purpose, status | Model calls, failed ones included |
| `llm_tokens_total` | counter | provider, kind (`input`, `output`, `thinking`) | Tokens reported by the provider |
| `llm_estimated_cost_usd_total` | counter | provider | Estimated cost at the configured `LLM_PRICING` (0 for local models) |
| `llm_call_duration_seconds` | histogram | provider, purpose | Model latency |
| `agent_run_duration_seconds` | histogram | status | Investigation creation to end |
| `agent_tool_calls_total` | counter | tool, channel, status | Agent and MCP tool calls |
| `extraction_confidence` | histogram | review_level | Overall confidence of stored extractions |

Gauges read from the database when the API is scraped (`docintel.observability.snapshot`):

| Metric | Labels | Meaning |
|---|---|---|
| `jobs` | job_type, status (`QUEUED`, `PROCESSING`) | Jobs waiting or running |
| `jobs_oldest_ready_age_seconds` | job_type | How long the oldest runnable job has waited (jobs waiting for a retry are not runnable yet) |
| `jobs_expired_leases` | — | Running jobs whose worker stopped renewing the lease |
| `jobs_failed_last_hour` | job_type | Jobs that failed permanently in the last hour |
| `documents` | status | Documents (not deleted) by status |
| `review_tasks_open` / `review_tasks_overdue` | priority | Review backlog and tasks past their due date |
| `workflows_active` | status | Workflows queued, running or awaiting approval |
| `llm_calls_today` | provider, status | Model calls since midnight UTC across all processes |
| `llm_estimated_cost_usd_today` | provider | Estimated cost since midnight UTC |
| `llm_daily_request_budget` | — | `LLM_DAILY_REQUEST_BUDGET` (0 = none) |
| `snapshot_success` | — | 1 if these gauges were read; 0 means the API could not read the database (the scrape still succeeds) |
| `snapshot_duration_seconds` | — | Time spent reading them |

Every label value of these gauges is reported even at 0, so an alert on a series cannot go
silent because the series disappeared.

## Running Prometheus and Grafana

```bash
# local stack (no token needed)
GRAFANA_ADMIN_PASSWORD=... docker compose -f docker-compose.yml \
  -f deploy/compose.observability.yml up -d prometheus grafana
# production host: add -f deploy/compose.prod.yml; METRICS_TOKEN and GRAFANA_ADMIN_PASSWORD
# come from .env
```

Prometheus listens on `127.0.0.1:9090` and Grafana on `127.0.0.1:3000` (reach them through an
SSH tunnel on a server). Grafana provisions the Prometheus data source and the dashboard
**Document Intelligence – operations** (folder *Document Intelligence*): overview stats, API
traffic, latency and refusals, processing (job outcomes, stage and OCR times, queue depth,
documents by status, extraction confidence), AI (calls, tokens, budget, estimated cost, tool
calls, investigation time) and database statement timings. Anonymous access and sign-up are off.

## Alerts

Defined in `deploy/observability/alerts.yml`, unit-tested with `promtool test rules`
(`deploy/observability/alerts.test.yml`). Each links to its runbook.

| Alert | Condition | Severity | Runbook |
|---|---|---|---|
| DocintelApiDown | an API replica fails scrapes for 2 min | critical | [api-down](runbooks.md#api-down) |
| DocintelWorkerDown | no worker answers for 5 min | critical | [worker-down](runbooks.md#worker-down) |
| DocintelDatabaseUnreadable | the API cannot read the database for 5 min | critical | [database-unavailable](runbooks.md#database-unavailable) |
| DocintelHighServerErrorRate | > 5 % of requests are 5xx for 5 min | critical | [server-errors](runbooks.md#server-errors) |
| DocintelSlowReads | GET p95 > 300 ms for 10 min (NFR-09) | warning | [slow-api](runbooks.md#slow-api) |
| DocintelRateLimiting | > 50 refusals in 10 min | info | [rate-limited](runbooks.md#rate-limited) |
| DocintelQueueBacklog | a runnable job waited > 10 min | warning | [queue-backlog](runbooks.md#queue-backlog) |
| DocintelJobsFailing | ≥ 3 permanent job failures in the last hour | warning | [failed-jobs](runbooks.md#failed-jobs) |
| DocintelStuckJobs | expired leases for 15 min | warning | [worker-down](runbooks.md#worker-down) |
| DocintelSlowScannedPages | OCR p95 per page > 5 s for 30 min (NFR-09) | warning | [slow-processing](runbooks.md#slow-processing) |
| DocintelReviewsOverdue | urgent/high review tasks past due for 1 h | warning | [review-backlog](runbooks.md#review-backlog) |
| DocintelModelBudgetNearlyUsed | > 80 % of today's model request budget | warning | [model-budget](runbooks.md#model-budget) |
| DocintelModelErrors | > 20 % of model calls fail for 15 min | warning | [model-errors](runbooks.md#model-errors) |

Alerts appear in Prometheus (`/alerts`) and Grafana. Sending them somewhere (e-mail, chat,
paging) needs an Alertmanager with a receiver; none is configured, because the receiver is
specific to the organisation running the platform.

## Logs

Every process logs JSON lines (structlog) in staging and production, with the request id
(`X-Request-ID`, also returned to the client) on every line of a request. Logs carry no
document content, no prompts and no secrets ([security architecture](../architecture/09-security-architecture.md)). `docker compose logs -f api worker`
follows them; any collector that reads container stdout can ship them.
