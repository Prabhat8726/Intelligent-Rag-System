# Runbooks

What to do when an alert fires ([monitoring](monitoring.md)) or an operation is due. Commands
assume the production layout on the host (`deploy/`), run from the repository checkout:

```bash
alias dc='docker compose -f docker-compose.yml -f deploy/compose.prod.yml'
```

Locally, use `docker compose` alone. Never paste secrets into tickets or chat: they live in the
host's `.env` (mode 600) only.

## api-down

An API replica stopped answering scrapes.

1. `dc ps api` and `dc logs --tail 200 api`: a crash at start is almost always configuration
   (the settings validation names the variable, e.g. `METRICS_TOKEN is required outside
   local/test`) or the database (`/health/ready` says which check fails).
2. The container restarts by itself (`restart: always`); nginx keeps sending requests to the
   other replicas, which it resolves every 10 s.
3. Memory: `docker stats` against the 1 GB limit. An OOM-killed replica shows exit code 137.
4. Still down after a fix: `dc up -d --wait api`.

## worker-down

No worker answers, or running jobs hold expired leases (`DocintelStuckJobs`). Documents stay
`PENDING`; uploads still succeed.

1. `dc ps worker`, `dc logs --tail 200 worker`. The worker refuses to start without Tesseract
   (`check-ocr`) or a database.
2. Expired leases are reclaimed by any running worker; a job reclaimed more than
   `JOB_MAX_ATTEMPTS` times is failed with "interrupted repeatedly" instead of looping.
3. Restart: `dc up -d --wait worker`. Scale: `WORKER_REPLICAS=2` in `.env`, then
   `dc up -d --wait worker` (claims use `FOR UPDATE SKIP LOCKED`, replicas never share a job).

## database-unavailable

The API cannot read the database (`docintel_snapshot_success 0`, `/health/ready` 503).

1. `dc ps db`, `dc logs --tail 100 db`; disk space on the host (`df -h`): PostgreSQL stops
   accepting writes when its volume is full.
2. Connections: `dc exec db psql -U docintel -c "select count(*), state from pg_stat_activity
   group by state"`. Each API replica and worker holds at most `DB_POOL_SIZE + DB_MAX_OVERFLOW`
   (default 20) connections; PostgreSQL allows 100 by default.
3. Data loss: restore from the newest dump (see [restore a backup](#restore-a-backup)).

## server-errors

More than 5 % of requests end in a 5xx.

1. Grafana *Requests per second by route* shows which route; the logs carry the request id
   of each failure: `dc logs api | grep '"level": "error"'`.
2. 502/504 from nginx means no API replica answered in time: see [api-down](#api-down) and
   [slow-api](#slow-api).
3. If it started with a release, roll back: `deploy/deploy.sh <previous tag>` (each deploy
   prints `deploy: <previous> -> <new>`; the deploy workflow runs list the tags too).

## slow-api

Read p95 above 300 ms (NFR-09).

1. *p95 latency by route*: one route, or all of them? All of them with high CPU on the host
   means too few API processes for the traffic: raise `API_REPLICAS` (each replica is one
   process; measured effect in [load-testing](load-testing.md)).
2. One route: *Statement p95 by verb* and `pg_stat_statements` (if enabled) show slow queries;
   `EXPLAIN ANALYZE` the query from the log of a slow request.
3. Workers compete for the same CPUs: during heavy ingestion, reads slow down. Move workers to
   another host, or lower `WORKER_CONCURRENCY`.

## rate-limited

Many requests refused with 429.

1. `docintel_rate_limited_requests_total` by `scope` says which limit. A script uploading a
   dataset should wait as `Retry-After` says (the CLI tools do).
2. A real user hitting `login` limits from a shared address (an office NAT): raise
   `RATE_LIMIT_LOGIN_PER_MINUTE`. An attack: the limits are doing their job; the per-account
   lockout (`AUTH_MAX_FAILED_LOGINS`) also applies.
3. 429 answered by nginx (problem detail "Too many requests from this address") is the edge
   limit: `NGINX_API_RATE` / `NGINX_API_BURST` on the web container.

## queue-backlog

A runnable job has waited more than 10 minutes.

1. Is a worker running ([worker-down](#worker-down))? Is it busy? *Job attempts by outcome*
   and *Queue depth*.
2. Scanned documents cost about ten times a native one (OCR); a burst of scans fills the
   queue. Add worker replicas, or raise `WORKER_CONCURRENCY` if the host has idle CPUs.
3. Jobs retried with backoff (`retried` outcomes) are not "runnable" until their `run_after`;
   they do not trigger this alert.

## failed-jobs

Three or more jobs failed permanently in the last hour.

1. The document detail page shows `processing_error`; the audit log has
   `document.processing.failed` with the job id; the worker log has the exception (search the
   job id).
2. A failed integrity check means the stored file changed or is missing:
   [reconcile storage](#reconcile-storage).
3. After fixing the cause, re-queue from the document page or `POST
   /api/v1/documents/{id}/process`.

## slow-processing

OCR p95 per page above 5 s (NFR-09).

1. CPU on the host: OCR is CPU-bound, Tesseract runs `OCR_CONCURRENCY` pages at a time per
   worker. Contention with the API or the database shows as all stages slowing.
2. `OCR_DPI` above 300 or large page images cost more; `OCR_UPSCALE_BELOW_DPI` upscales
   small scans (2x at most).

## review-backlog

Urgent or high-priority review tasks are past their due date. Not a technical fault: tell
the review team's manager; the review queue sorts by due date. Due dates come from
`REVIEW_SLA_HOURS`.

## model-budget

More than 80 % of `LLM_DAILY_REQUEST_BUDGET` is used. At 100 % model calls stop until
midnight UTC and processing continues with local methods only (layout extraction,
classification, deterministic agent planning), which send more documents to review. Raise the
budget only if the provider quota and the cost allow; `docintel llm-usage` shows the
consumption per day.

## model-errors

More than 20 % of model calls fail. `docintel check-ai` tests the key and the models.
Provider outages and quota errors (429) fall back to local methods; nothing needs to be
retried by hand. An invalid key fails every call: rotate it ([rotate a
secret](#rotate-a-secret)).

---

## Deploy a release

`.github/workflows/deploy.yml` (manual, per GitHub environment) or on the host:

```bash
git fetch --tags origin && git checkout --detach <commit or v-tag>
deploy/deploy.sh sha-<first 7 characters of the commit>
```

The script pulls the images, dumps the database (`pre-<tag>-<time>.dump`), runs the
migrations, starts the new containers, waits for every health check and checks
`https://$DOMAIN/health/ready`. If the new release does not become ready it starts the previous
one again. Migrations are additive (new tables and nullable columns; nothing the previous
release reads is dropped or renamed in the same release), so the previous release runs on the
new schema. A release that must remove or rename something does it in two releases: stop using
it, then drop it.

## Roll back

`deploy/deploy.sh <previous tag>`. If a migration of the bad release must be undone, restore
the `pre-<bad tag>-*.dump` taken before it migrated (below); data written since is lost, so
prefer fixing forward when the schema change is additive.

## Restore a backup

Dumps are in the `backups` volume (`dc exec backup ls -l /backups`), one per
`BACKUP_INTERVAL_HOURS`, kept `BACKUP_KEEP_DAYS`, plus one before each deploy.

```bash
dc stop api worker maintenance                    # nothing writes during the restore
dc exec backup pg_restore --clean --if-exists --no-owner -d docintel /backups/<file>.dump
dc up -d --wait
```

Document files are not in the dump: they live in the `docstore` volume (or the S3 bucket).
Back that up with the host's volume backups or bucket versioning; after restoring an older
database, [reconcile storage](#reconcile-storage) reports files the database no longer knows.
**Restore drills**: restoring the newest dump into a scratch database
(`createdb restore_check && pg_restore -d restore_check ...`) monthly is the only proof the
backups work.

## Reconcile storage

```bash
dc exec api docintel storage-reconcile                     # report only
dc exec api docintel storage-reconcile --delete-orphans    # delete unreferenced files
```

Orphans (files no row refers to, older than 24 h) are left by interrupted uploads and failed
deletions; the maintenance service deletes them daily. **Missing** files (rows whose file is
gone) are data loss: restore the file from the storage backup; the command exits non-zero
while any are missing.

## Purge deleted documents

Deleted documents are hidden at once and removed for good `RETENTION_DELETED_DAYS` (30) days
later by the maintenance service (`docintel purge-deleted`; `--dry-run` counts). Each purge is
an audit event (`document.purged`). Reports already generated keep their snapshot.

## Rotate a secret

* `JWT_SECRET_KEY`: change it in `.env` and `dc up -d api`; every access token and browser
  session ends (everyone signs in again).
* `METRICS_TOKEN`: change it in `.env`, then `dc up -d api worker prometheus` together.
* `POSTGRES_PASSWORD`: `dc exec db psql -U docintel -c "ALTER USER docintel PASSWORD '...'"`,
  then change `.env` and `dc up -d`.
* `GEMINI_API_KEY`: replace it in `.env` (never in chat or tickets) and `dc up -d api worker`.
* API tokens (MCP clients) are revoked by their owner on the API tokens page.

## Create the first administrator

Seeding demo users is refused in staging and production. On the host:

```bash
dc exec -T api docintel create-user --email you@example.com --full-name "Your Name" \
  --role ADMIN --password-stdin < password-file
```
