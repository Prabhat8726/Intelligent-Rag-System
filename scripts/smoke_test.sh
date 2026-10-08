#!/usr/bin/env bash
# Smoke test for the Docker stack, exercised through nginx exactly like a browser would.
# Usage: ./scripts/smoke_test.sh            (expects `make up` to have been run)
# If SEED_USER_PASSWORD is set in .env, demo users are seeded and a full login is verified.
set -euo pipefail

cd "$(dirname "$0")/.."
WEB_PORT="${WEB_PORT:-$(grep -E '^WEB_PORT=' .env 2>/dev/null | cut -d= -f2 || true)}"
BASE_URL="http://127.0.0.1:${WEB_PORT:-8080}"
SEED_PASSWORD="$(grep -E '^SEED_USER_PASSWORD=' .env 2>/dev/null | cut -d= -f2- || true)"

pass() { printf '  \033[32mPASS\033[0m %s\n' "$1"; }
fail() { printf '  \033[31mFAIL\033[0m %s\n' "$1"; exit 1; }

# check <description> <command...>: passes when the command succeeds.
check() {
  local description="$1"
  shift
  if "$@"; then pass "$description"; else fail "$description"; fi
}

quiet() { "$@" >/dev/null 2>&1; }

status_of() { curl -s -o /dev/null -w '%{http_code}' "$1"; }

expect_status() { # url expected_status description
  local status
  status="$(status_of "$1")"
  check "$3 (expected $2, got $status)" test "$status" = "$2"
}

echo "Smoke testing ${BASE_URL}"
expect_status "${BASE_URL}/nginx-health" 200 "nginx is up"
expect_status "${BASE_URL}/health" 200 "API liveness via proxy"
expect_status "${BASE_URL}/health/ready" 200 "API readiness (database + migrations)"
expect_status "${BASE_URL}/status" 200 "SPA deep link falls back to index.html"
expect_status "${BASE_URL}/api/v1/auth/me" 401 "protected endpoint rejects anonymous calls"

headers="$(curl -s -D - -o /dev/null "${BASE_URL}/")"
check "SPA served with Content-Security-Policy" \
  grep -qi "content-security-policy: default-src 'self'" <<<"$headers"
check "SPA served with X-Frame-Options" grep -qi "x-frame-options: DENY" <<<"$headers"

bad_login="$(curl -s -X POST "${BASE_URL}/api/v1/auth/login" -H 'content-type: application/json' \
  -d '{"email":"nobody@example.test","password":"wrong password"}')"
check "invalid login returns a problem detail" grep -q '"Invalid email or password."' <<<"$bad_login"

if [[ -n "${SEED_PASSWORD}" ]]; then
  check "demo users seeded" quiet docker compose exec -T api docintel seed
  token="$(curl -s -X POST "${BASE_URL}/api/v1/auth/login" -H 'content-type: application/json' \
    -d "{\"email\":\"analyst@docintel.local\",\"password\":\"${SEED_PASSWORD}\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("access_token", ""))')"
  check "login with the seeded analyst" test -n "$token"
  role="$(curl -s "${BASE_URL}/api/v1/auth/me" -H "Authorization: Bearer ${token}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin).get("role", ""))')"
  check "/auth/me through nginx returns the analyst role" test "$role" = "ANALYST"
else
  echo "  SKIP login round-trip (SEED_USER_PASSWORD not set in .env)"
fi

echo "All smoke checks passed."
