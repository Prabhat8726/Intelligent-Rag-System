#!/bin/sh
# Nightly logical backups of the database (deploy/compose.prod.yml, service "backup").
# Custom-format dumps (pg_restore can restore one table or the whole database), written to a
# temporary name and renamed when complete, so a partial dump is never mistaken for a backup.
set -eu

interval_hours="${BACKUP_INTERVAL_HOURS:-24}"
keep_days="${BACKUP_KEEP_DAYS:-14}"

until pg_isready -q; do sleep 2; done
while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  target="/backups/${PGDATABASE}-${stamp}.dump"
  if pg_dump --format=custom --no-owner --file="${target}.partial"; then
    mv "${target}.partial" "${target}"
    echo "backup: wrote ${target} ($(du -h "${target}" | cut -f1))"
  else
    rm -f "${target}.partial"
    echo "backup: pg_dump FAILED at ${stamp}" >&2
  fi
  find /backups -name '*.dump' -mtime "+${keep_days}" -print -delete
  sleep "$((interval_hours * 3600))"
done
