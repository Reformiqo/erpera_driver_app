#!/usr/bin/env bash
# erpera_driver_app — one-shot deploy script.
#
# Runs the full sequence to bring a Frappe site up-to-date with the
# latest main branch of erpera_driver_app. Idempotent — safe to re-run
# on every push; will skip operations that are already done.
#
# Usage:
#     ./scripts/deploy.sh <site> [bench-path]
#
# Examples:
#     ./scripts/deploy.sh dev.localhost
#     ./scripts/deploy.sh cowberry.frappe.cloud ~/frappe-bench
#
# Environment overrides:
#     APP_NAME   — defaults to "erpera_driver_app"
#     REPO_URL   — defaults to the public GitHub HTTPS URL
#     BRANCH     — defaults to "main"
#     SKIP_PULL  — set to 1 to skip the git pull (useful in CI where the
#                  code is already on disk)

set -euo pipefail

SITE="${1:-}"
BENCH_PATH="${2:-$HOME/frappe-bench}"
APP_NAME="${APP_NAME:-erpera_driver_app}"
REPO_URL="${REPO_URL:-https://github.com/reformiqo/erpera_driver_app}"
BRANCH="${BRANCH:-main}"
SKIP_PULL="${SKIP_PULL:-0}"

if [[ -z "$SITE" ]]; then
    cat <<EOF
Usage: $0 <site> [bench-path]

Examples:
    $0 dev.localhost
    $0 cowberry.frappe.cloud ~/frappe-bench
EOF
    exit 1
fi

if [[ ! -d "$BENCH_PATH" ]]; then
    echo "✗ Bench path not found: $BENCH_PATH" >&2
    exit 1
fi

cd "$BENCH_PATH"

echo "── 1/5: ensuring app code is on disk ──"
if [[ -d "apps/$APP_NAME" ]]; then
    if [[ "$SKIP_PULL" == "1" ]]; then
        echo "  skip (SKIP_PULL=1)"
    else
        echo "  pulling latest $BRANCH …"
        (cd "apps/$APP_NAME" && git fetch origin "$BRANCH" && git checkout "$BRANCH" && git pull origin "$BRANCH")
    fi
else
    echo "  cloning $REPO_URL …"
    bench get-app "$REPO_URL" --branch "$BRANCH"
fi

echo "── 2/5: checking if app is installed on $SITE ──"
if bench --site "$SITE" list-apps 2>/dev/null | grep -q "^$APP_NAME"; then
    echo "  already installed — skipping install-app"
else
    echo "  running install-app …"
    bench --site "$SITE" install-app "$APP_NAME"
fi

echo "── 3/5: running migrations on $SITE ──"
bench --site "$SITE" migrate

echo "── 4/5: restarting workers (reloads Python modules) ──"
bench restart

echo "── 5/5: smoke-testing auth.app_version ──"
HOST="${SITE}"
RESPONSE=$(curl -sS -o /dev/null -w "%{http_code}" \
    "https://${HOST}/api/method/${APP_NAME}.api.auth.app_version" \
    || echo "000")
if [[ "$RESPONSE" == "200" ]]; then
    echo "  ✓ auth.app_version returns 200 — deploy looks good"
else
    echo "  ⚠ auth.app_version returned HTTP $RESPONSE"
    echo "    (200 = deployed cleanly; 417 = workers still cached, retry restart;"
    echo "     404 = site / DNS issue; 5xx = check bench/web.error.log)"
fi

echo ""
echo "✓ deploy.sh finished for $APP_NAME on $SITE"
