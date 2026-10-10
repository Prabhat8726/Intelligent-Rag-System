# Load testing

NFR-09 asks a deployment for: read API p95 ≤ 300 ms at the expected concurrency; processing
p95 ≤ 1 s per native document and ≤ 5 s per scanned page; no failed job on valid input. Phase 10
measured the components in process (ADR-071); this is the measurement through the deployed
shape: nginx in front of one or more API containers, a worker, PostgreSQL, all hardened as in
`docker-compose.yml` (ADR-080).

## How to run it

```bash
API_REPLICAS=2 docker compose -f docker-compose.yml -f deploy/compose.loadtest.yml up -d --wait
make seed-docker && make process API_URL=http://localhost:8080    # documents to read
make loadtest LOADTEST_ARGS="--users 8 --duration 60 --label api_replicas=2"
# or directly, e.g. with uploads during the run:
cd backend && uv run --env-file ../.env docintel loadtest --api-url http://localhost:8080 \
  --users 8 --dataset ../synthetic_data/generated --output ../evaluation/load/my-run.json
```

* Each virtual user loops over the pages people open most: inbox, a document, its extraction
  and findings, the review queue, the dashboard. Latency is measured at the client, through
  nginx; a 5 s warm-up is not recorded.
* With `--dataset`, the directory is uploaded through the API while the readers run, and each
  document's processing time is read back from its job: native documents per document,
  scanned (and mixed) documents per page.
* `deploy/compose.loadtest.yml` raises the nginx per-address limit and the per-user upload
  limit: every virtual user comes from one address and one account. Everything else is the
  normal configuration.
* The command exits non-zero when a target is missed, and writes JSON and Markdown reports.

## Results

Measured on 2026-10-10 on one 4-CPU Linux machine running everything: PostgreSQL, nginx, the
API replicas, one worker (concurrency 1) and the load generator itself, so the stack had less
than 4 CPUs. Deterministic mode (no model calls; hashing embeddings). The dataset is 131
synthetic documents (105 native, 26 scanned: 20 rescans of native documents at 150 DPI plus
the generator's scans); the database held 132 documents for the read runs and up to 394 for
the mixed ones (each mixed run uploads the dataset again). Raw reports: `evaluation/load/`.

| Run | API replicas | Users | Reads/s | Read p50 | Read p95 | Read p99 | Native p95 | Scanned p95 / page | Failed jobs |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| reads | 1 | 8 | 58.0 | 127 ms | **207 ms** | 330 ms | – | – | – |
| reads | 2 | 8 | 90.0 | 83 ms | **167 ms** | 266 ms | – | – | – |
| reads | 2 | 16 | 89.0 | 164 ms | 386 ms | 530 ms | – | – | – |
| reads | 3 | 16 | 110.7 | 126 ms | 319 ms | 457 ms | – | – | – |
| reads + processing | 1 | 8 | 49.7 | 146 ms | **245 ms** | 355 ms | **0.271 s** | **2.225 s** | **0 / 131** |
| reads + processing | 2 | 8 | 72.8 | 101 ms | **214 ms** | 326 ms | **0.358 s** | **2.306 s** | **0 / 131** |

Every request answered 200; there were no transport errors and no 429.

**NFR-09 verdict on this machine.** With 8 concurrent users (the concurrency Phase 10
identified), every target is met with one API process or more, including while documents are
being processed. With 16 concurrent users the read target is not met: p95 386 ms with two
replicas and 319 ms with three, because the four CPUs are saturated (one sample per run, 40 s in:
the API processes together 100-200 %, PostgreSQL 50-83 %, the worker 61 % while processing,
plus the load generator). More API replicas only help when there are CPUs for them; on a
deployment host without the load generator, or with more CPUs, the 16-user point needs to be
measured again rather than extrapolated.

## What the load test found and changed

**The database spent three times longer planning than executing.** With
`pg_stat_statements` (planning tracked) during an 8-user, 2-replica run, planning took 16.0 s
against 5.7 s of execution in 30 s. The driver prepares statements, but PostgreSQL re-plans
these parameterised `ORDER BY … LIMIT` queries on every execution (custom plans win over the
generic plan), so the cost of planning a statement is paid on every request. The review queue
statement joined 11 tables, because every user on a task (assignee, resolver, document owner)
is loaded eagerly together with its department: 9.1 ms of planning for 2.7 ms of execution.
The listings of the review queue and the inbox now load people in small batched queries
(`selectinload`) instead: planning 9.1 → 1.7 ms and 2.5 → 0.7 ms per call, execution 2.7 → 1.8
ms and 1.8 → 1.3 ms. Measured effect on the same data and machine (before → after):

| Run | Reads/s | Read p95 |
|---|---|---|
| reads, 1 replica, 8 users | 46.7 → 58.0 | 284 → 207 ms |
| reads, 2 replicas, 8 users | 79.2 → 90.0 | 193 → 167 ms |
| reads, 3 replicas, 16 users | 89.4 → 110.7 | 398 → 319 ms |
| reads + processing, 1 replica, 8 users | 43.4 → 49.7 | 305 → 245 ms (target missed before) |

Forcing generic plans for every statement (`plan_cache_mode`) was not chosen: a generic plan
cannot use the partial indexes the job queue relies on (`WHERE status = 'QUEUED'`).

**A read costs about 22 ms of API CPU** (ORM, validation, JSON) and about eight statements;
unloaded, every page answers in 25-50 ms. Under load the rest is queueing, which is why one
API process per container, scaled out with `API_REPLICAS`, is the deployment model (ADR-078).

## Not measured

* A separate load-generator machine, a deployment host with more CPUs, or managed PostgreSQL.
* Model-backed paths (questions, investigations with a model): they are bounded by the
  provider's latency and quota, not by this stack, and the free tier must not be load-tested.
* Long runs (hours), memory growth over days, and more than one worker host.
