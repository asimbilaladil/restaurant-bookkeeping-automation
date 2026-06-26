#!/usr/bin/env python3
"""
Daily Sales Reconciliation — automated run for ALL locations.

Intended to be run by cron once a day (04:00 UTC). It fetches the previous
day's Revel operations data for every establishment and posts/approves each
location's R365 journal entry, recording each run exactly like the interactive
/api/r365/reconcile-all flow does.

Run manually for a specific date:
    venv/bin/python3 daily_reconcile.py 2026-06-25

With no argument it uses "yesterday" in UTC (the day that just completed).
"""

import logging
import sys
from datetime import date, datetime, timedelta, timezone

# Importing server runs load_dotenv()/db.init_db() and configures logging.
# We reuse its helpers (_extract_revel_values, _record_run, _entity_log) so the
# recorded runs are identical to the web flow.
import server
from revel import fetch_reports, DEFAULT_ESTABLISHMENTS, ESTABLISHMENT_NAMES, R365_NAME_OVERRIDES
from r365 import open_r365_journal_entry

log = logging.getLogger("daily_reconcile")


def _target_date() -> date:
    if len(sys.argv) > 1:
        return date.fromisoformat(sys.argv[1])
    # "a date before" → yesterday in UTC
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def main() -> int:
    target_date = _target_date()
    log.info("=== Daily reconcile START — date=%s, all locations ===", target_date)

    results = fetch_reports(target_date, DEFAULT_ESTABLISHMENTS)
    log.info("Fetched Revel data for %d establishment(s)", len(results))

    ok = failed = errored = 0
    for r in results:
        est_id = r.get("establishment_id")
        name = ESTABLISHMENT_NAMES.get(est_id, str(est_id))

        if r.get("error") or not r.get("data"):
            errored += 1
            msg = r.get("error") or "no data returned"
            log.warning("est %s (%s) — fetch failed: %s", est_id, name, msg)
            server._record_run(est_id, name, target_date, "error", error=msg)
            continue

        revel_values = server._extract_revel_values(r["data"])
        r365_name = R365_NAME_OVERRIDES.get(est_id, name)
        pdf_path = r.get("pdf_path")

        try:
            with server._entity_log(name, target_date) as log_path:
                result = open_r365_journal_entry(
                    target_date, r365_name, revel_values, pdf_path
                )
            if "error" in result:
                errored += 1
                log.error("est %s (%s) → error: %s", est_id, name, result["error"])
                server._record_run(est_id, name, target_date, "error",
                                   error=result["error"], log_filename=log_path.name)
            else:
                je_balanced = result.get("je_balanced", True)
                je_difference = result.get("je_difference", 0.0)
                # Success means the JE actually balances (same rule as the web flow).
                run_status = "success" if je_balanced else "failed"
                ok += int(je_balanced)
                failed += int(not je_balanced)
                log.info("est %s (%s) → %s (diff=%.2f)",
                         est_id, name, run_status, je_difference)
                server._record_run(est_id, name, target_date, run_status,
                                   je_difference=je_difference,
                                   je_balanced=je_balanced,
                                   attachment_status=result.get("attachment_status", "skipped"),
                                   log_filename=log_path.name)
        except Exception as exc:
            errored += 1
            log.error("est %s (%s) → exception: %s", est_id, name, exc)
            server._record_run(est_id, name, target_date, "error", error=str(exc))

    log.info("=== Daily reconcile DONE — date=%s: %d balanced, %d unbalanced, %d errored ===",
             target_date, ok, failed, errored)
    return 0


if __name__ == "__main__":
    sys.exit(main())
