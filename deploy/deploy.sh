#!/usr/bin/env bash
# Deploy a release to this host (ADR-078). Run from the repository checkout on the host:
#   deploy/deploy.sh [--no-pull] <image-tag>
# --no-pull: the images are already on this host (`docker load` on an air-gapped host).
# 1. pull the release images; 2. back up the database; 3. run migrations; 4. start the new
# containers and wait for their health checks; 5. check readiness through the public URL.
# If 4 or 5 fails, the previous release's containers are started again (migrations are
# additive, so the previous release runs on the new schema; see the release runbook).
set -euo pipefail

pull=1
if [[ "${1:-}" == "--no-pull" ]]; then pull=0; shift; fi
tag="${1:?usage: deploy/deploy.sh [--no-pull] <image-tag>}"
cd "$(dirname "$0")/.."
test -f .env || { echo "deploy: .env is missing (start from deploy/production.env.example)"; exit 1; }

compose=(docker compose -f docker-compose.yml -f deploy/compose.prod.yml)
domain="$(grep -E '^DOMAIN=' .env | cut -d= -f2-)"
previous="$(cat .deployed-tag 2>/dev/null || true)"

run() { IMAGE_TAG="$1" "${compose[@]}" "${@:2}"; }

echo "deploy: ${previous:-nothing} -> ${tag}"
if (( pull )); then
  run "$tag" pull --quiet migrate api worker web maintenance
fi

if [[ -n "$previous" ]]; then
  echo "deploy: backing up the database before migrating"
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  run "$previous" exec -T backup sh -c \
    "pg_dump --format=custom --no-owner --file=/backups/pre-${tag}-${stamp}.dump"
fi

echo "deploy: migrating"
run "$tag" run --rm migrate

healthy() {
  run "$tag" up -d --wait --wait-timeout 180 --remove-orphans \
    && curl --fail --silent --show-error --max-time 10 "https://${domain}/health/ready" >/dev/null
}

if healthy; then
  echo "$tag" > .deployed-tag
  echo "deploy: ${tag} is live"
  exit 0
fi

echo "deploy: ${tag} did not become ready" >&2
if [[ -n "$previous" ]]; then
  echo "deploy: starting ${previous} again" >&2
  run "$previous" up -d --wait --wait-timeout 180 --remove-orphans
fi
exit 1
