#!/usr/bin/env bash
#
# Runs ON the BI host. Updates the checkout to a ref, rebuilds, waits for
# health, and prints a rollback command if it fails.
#
#     bash scripts/host-update.sh [ref]      # default: origin/main
#
# Kept separate from deploy.sh so the same logic serves both the manual SSH
# deploy and an automated (SSM) one, rather than being written twice.
set -euo pipefail

REF="${1:-origin/main}"
APP_DIR="${APP_DIR:-$HOME/data-mesh}"
CONTAINER="${CONTAINER:-report-hub}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/healthz}"

cd "$APP_DIR"

PREVIOUS="$(git rev-parse --short HEAD)"
echo "currently deployed: $PREVIOUS"

# ── preflight: config the app cannot start without ──────────────────────────
missing=""
for key in SESSION_SECRET DB_HOST DB_USER DB_PASSWORD AUTH_MODE; do
    grep -qE "^${key}=.+" .env || missing="$missing $key"
done
if [ -n "$missing" ]; then
    echo "ABORT: .env is missing required keys:$missing" >&2
    exit 1
fi

# AUTH_MODE=sso/both with no Keycloak client means nobody can log in. That
# exact combination shipped once and left the app crash-looping, so it's a
# hard stop rather than a warning.
if grep -qE '^AUTH_MODE=(sso|both)$' .env; then
    if ! (grep -qE '^SSO_CLIENT_ID=.+' .env && grep -qE '^SSO_CLIENT_SECRET=.+' .env); then
        echo "ABORT: AUTH_MODE is sso/both but SSO_CLIENT_ID/SSO_CLIENT_SECRET are not both set" >&2
        exit 1
    fi
fi

# Charts self-disable without the app database, so this is only a note.
grep -qE '^APP_DB_PASSWORD=.+' .env \
    || echo "NOTE: APP_DB_PASSWORD unset — the Charts tab will show 'not configured'"

# ── fetch the target revision ───────────────────────────────────────────────
git fetch --all --tags --quiet
git checkout --quiet --detach "$REF"
TARGET="$(git rev-parse --short HEAD)"
echo "deploying: $TARGET"

# ── rebuild ─────────────────────────────────────────────────────────────────
# `up -d --build`, never `restart`: docker only re-reads env_file when the
# container is recreated, so a restart silently keeps the old environment.
docker compose up -d --build

# ── wait for health ─────────────────────────────────────────────────────────
ok=""
for _ in $(seq 1 30); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "$HEALTH_URL" || true)"
    if [ "$code" = "200" ]; then ok="yes"; break; fi
    sleep 2
done

if [ -z "$ok" ]; then
    {
        echo ""
        echo "DEPLOY FAILED: $HEALTH_URL never returned 200"
        echo "--- last 40 log lines ---"
        docker logs "$CONTAINER" --tail 40 2>&1 || true
        echo ""
        echo "roll back on the host with:"
        echo "  cd $APP_DIR && git checkout --detach $PREVIOUS && docker compose up -d --build"
    } >&2
    exit 1
fi

echo ""
echo "healthy at $TARGET (was $PREVIOUS)"
docker ps --format '{{.Names}}\t{{.Status}}' | grep "$CONTAINER" || true
docker logs "$CONTAINER" 2>&1 | grep -iE 'Auth:|App database|No login method' | tail -5 || true
