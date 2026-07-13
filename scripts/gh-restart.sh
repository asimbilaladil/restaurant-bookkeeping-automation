#!/usr/bin/env bash
#
# gh-restart.sh — restart revel-r365.service, optionally flipping the
# NET_CASH_REFUNDS feature flag (commit 4e2b1c0) first.
#
# Invoked by the "Restart Server" GitHub Actions workflow via a narrow sudo
# rule. Runs as root. Lives in the live deployment dir (/opt/accounts/app),
# NOT the runner checkout, because it edits the .env the service actually reads.
#
# Usage: gh-restart.sh <unchanged|true|false>
set -euo pipefail

ENV_FILE=/opt/accounts/app/.env
SERVICE=revel-r365.service
FLAG=${1:-unchanged}

case "$FLAG" in
    unchanged)
        echo "NET_CASH_REFUNDS: leaving unchanged"
        ;;
    true|false)
        if grep -q '^NET_CASH_REFUNDS=' "$ENV_FILE"; then
            sed -i "s/^NET_CASH_REFUNDS=.*/NET_CASH_REFUNDS=$FLAG/" "$ENV_FILE"
        else
            printf '\n# Feature flag for commit 4e2b1c0 (net cash refunds out of 8000-06)\nNET_CASH_REFUNDS=%s\n' "$FLAG" >> "$ENV_FILE"
        fi
        echo "NET_CASH_REFUNDS set to $FLAG"
        ;;
    *)
        echo "ERROR: expected unchanged|true|false, got '$FLAG'" >&2
        exit 2
        ;;
esac

echo "Restarting $SERVICE ..."
systemctl restart "$SERVICE"
sleep 3
systemctl is-active "$SERVICE"
systemctl status "$SERVICE" --no-pager -l | head -12
