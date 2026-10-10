# Deployment

How the platform runs in each environment, how a release reaches a server, and what is
verified. Decisions: ADR-077 (container hardening), ADR-078 (single-host deployment, releases),
ADR-079 (monitoring). Day-two procedures: [runbooks](runbooks.md).

**Status: no staging or production deployment has been performed.** The configuration below
was rehearsed end to end on a local Docker host (see [Rehearsal](#rehearsal)); the release and
deploy workflows are written and lint-clean but have not run on GitHub, and no server exists.

## Environments

| | Local | Staging | Production |
|---|---|---|---|
| `APP_ENV` | `local` | `staging` | `production` |
| Compose files | `docker-compose.yml` | `+ deploy/compose.prod.yml` | `+ deploy/compose.prod.yml` |
| Images | built on the machine | pulled from GHCR (`sha-<commit>`) | the same image tags as staging |
| Edge | nginx on `127.0.0.1:8080`, plain HTTP | Caddy, HTTPS (Let's Encrypt), HTTP → HTTPS | same |
| Secrets | `.env` from `make env` | `.env` on the host (mode 600) | same, separate values |
| Insecure defaults | allowed | refused at start-up (placeholder JWT secret, `*` CORS, missing `METRICS_TOKEN`); Secure cookies, HSTS, no API docs in production | same |
| Demo data | `make seed`, synthetic only | `docintel seed` refused | refused |
| Backups, retention | none | nightly `pg_dump`, daily purge and reconciliation | same |

Settings that differ by environment are derived from `APP_ENV` in one place
(`docintel.core.config`); the code has no environment branches beyond those properties.

## Topology (one host)

```
Internet ──443──> caddy (TLS, HSTS, /metrics refused)
                    └──> web: nginx (SPA, per-address limits, real client address)
                            └──> api × API_REPLICAS (uvicorn, one process each)
                                     └──> db (PostgreSQL 17 + pgvector)   <── worker × WORKER_REPLICAS
backup (pg_dump loop) ──> db          maintenance (purge, reconcile) ──> db + storage
prometheus ──scrapes──> api:8000/metrics, worker:9100/metrics      grafana ──> prometheus
```

* Only Caddy publishes ports. The database, nginx, the metrics endpoints, Prometheus and
  Grafana are reachable on the host's private network or `127.0.0.1` only.
* The application containers run read-only, as non-root, with no Linux capabilities, no
  privilege escalation, a size-capped `/tmp`, and CPU, memory and process limits (ADR-077).
  Measured footprints (load test): API about 155 MB, worker about 315 MB while processing,
  against limits of 1 GB and 2 GB.
* Document files live in the `docstore` volume (`STORAGE_BACKEND=local`) or in any
  S3-compatible bucket (`STORAGE_BACKEND=s3`: AWS S3, Cloudflare R2, MinIO); PostgreSQL can be
  a managed service by pointing `DATABASE_URL` at it and removing the `db` service. These are the
  only cloud-specific choices and both are configuration behind the storage and database
  abstractions; nothing in the code names a cloud.

### Sizing and cost

The measured shape (4 CPUs shared by everything, [load testing](load-testing.md)) served 8
concurrent readers within NFR-09 with one API process while processing documents. A single
VM with 4 vCPUs and 8 GB of memory is therefore the starting point; scanned-document volume
decides the worker count, concurrent users the API replicas. Costs that grow with use:
model calls (bounded by `LLM_DAILY_REQUEST_BUDGET`; zero with local models), storage, and
backups. No price is quoted here: it depends on the provider and region chosen.

## First-time host setup

1. A Linux host with Docker Engine and the Compose plugin; DNS `A`/`AAAA` records of the domain
   pointing at it; ports 80 and 443 open (80 serves the ACME challenge and the HTTPS redirect).
2. A deploy user in the `docker` group, with an SSH key whose public half is in its
   `authorized_keys`; clone the repository (read-only deploy key or HTTPS).
3. `cp deploy/production.env.example .env && chmod 600 .env`, then set every
   `replace_with_...` value. Generate secrets with
   `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`. Never paste them into chat
   or tickets.
4. If the GHCR packages are private: `docker login ghcr.io` with a token that can only read
   packages.
5. First release: `deploy/deploy.sh sha-<commit>`, then create the first administrator
   ([runbook](runbooks.md#create-the-first-administrator)).
6. Monitoring: `docker compose -f docker-compose.yml -f deploy/compose.prod.yml -f
   deploy/compose.observability.yml up -d prometheus grafana`, and reach Grafana through
   `ssh -L 3000:127.0.0.1:3000 <host>`.

## Releases

`.github/workflows/release.yml` runs on every push to `main` and every `v*` tag:

1. builds the backend and web images;
2. scans them with Trivy: a table of high and critical vulnerabilities and a CycloneDX SBOM
   are kept as workflow artifacts; a **critical vulnerability that has a fix blocks the
   release**;
3. pushes `ghcr.io/<owner>/docintel-{backend,web}:sha-<commit>` (plus the version for `v*`
   tags and `main`) with build provenance.

`.github/workflows/deploy.yml` is started by hand with an environment (`staging` or
`production`) and a commit or tag. It checks that both images exist, then over SSH moves the
host's checkout to that commit and runs `deploy/deploy.sh`, which:

1. pulls the images;
2. dumps the database (`pre-<tag>-<time>.dump` in the backups volume);
3. runs the migrations;
4. starts the containers and waits for every health check;
5. checks `https://$DOMAIN/health/ready`;
6. if 4 or 5 fails, starts the previous release again and exits non-zero.

GitHub environments hold the SSH key and host details; give `production` required reviewers
so a deploy waits for approval. Staging and production use the same image tags: what was
tested in staging is what runs in production.

## Rehearsal

On 2026-10-10 the production layout ran on a local Docker host with `APP_ENV=staging`,
`DOMAIN=localhost`, a Caddy internal certificate authority, two API replicas, images tagged
locally (`deploy/deploy.sh --no-pull`), generated secrets and a token-protected metrics
endpoint. Observed:

* release `r1` became ready over HTTPS (`/health/ready` 200); `http://` redirected to
  `https://` (308); `/metrics` at the edge answered 404; HSTS and the SPA's CSP were present;
* a login sent with a forged `X-Forwarded-For` was audited with the real client address
  (Caddy drops the header, nginx trusts only Caddy);
* both metrics endpoints answered 401 without the token and served metrics with it;
* the `backup` service wrote a dump at start; release `r2` was preceded by a
  `pre-r2-*.dump`; restoring it into a scratch database gave the same tables, migration
  revision and rows;
* release `r3`, built with an API that exits at start, failed its health checks: the script
  started `r2` again, which answered ready, and `.deployed-tag` stayed `r2`;
* the maintenance loop ran the purge and the reconciliation;
* Prometheus discovered both API replicas and the worker and scraped them with the token;
  all 13 rules loaded; with the worker stopped, `DocintelWorkerDown` went pending within a
  scrape and fired after its 5 minutes; Grafana served the provisioned dashboard and refused
  anonymous access.

Found and fixed during the rehearsal: the maintenance container inherited the API's health
check (disabled for it); nginx's config directory on a tmpfs needed the nginx user as owner;
the forwarded scheme is set by the TLS proxy, not by nginx; Caddy tried to install its local
CA into the read-only container (`skip_install_trust`).

Not rehearsed: real certificates from Let's Encrypt, the GitHub workflows themselves (they
need a registry, runners and a host), the Trivy scan (the scanner could not be downloaded in
the build environment), and restoring the `docstore` volume.
