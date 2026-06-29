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
import db
import server
from revel import fetch_reports, DEFAULT_ESTABLISHMENTS, ESTABLISHMENT_NAMES, R365_NAME_OVERRIDES
from r365 import open_r365_journal_entry

log = logging.getLogger("daily_reconcile")


def _target_date() -> date:
    if len(sys.argv) > 1:
        return date.fromisoformat(sys.argv[1])
    # "a date before" → yesterday in UTC
    return (datetime.now(timezone.utc) - timedelta(days=1)).date()


def _pending_establishments(target_date: date) -> list[int]:
    """Establishments that do NOT yet have a successful run for target_date.

    This is what makes the job safe to re-run on a 30-minute cron: each pass
    only processes the locations still failing (e.g. because R365 hasn't
    imported their DSS yet — see the import-lag behaviour). Once a location
    succeeds it is skipped on every subsequent retry.
    """
    pending = []
    for est in DEFAULT_ESTABLISHMENTS:
        done = db.count_runs(run_date=str(target_date), establishment_id=est, status="success")
        if not done:
            pending.append(est)
    return pending


def main() -> int:
    target_date = _target_date()

    pending = _pending_establishments(target_date)
    if not pending:
        log.info("=== Daily reconcile — all %d locations already successful for %s; nothing to do ===",
                 len(DEFAULT_ESTABLISHMENTS), target_date)
        return 0

    log.info("=== Daily reconcile START — date=%s, %d/%d location(s) pending: %s ===",
             target_date, len(pending), len(DEFAULT_ESTABLISHMENTS),
             [ESTABLISHMENT_NAMES.get(e, e) for e in pending])

    results = fetch_reports(target_date, pending)
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
                approved = result.get("approved", "skipped")
                # Success means the JE balanced AND R365 accepted the approval.
                # A balanced-but-unapproved JE is saved in "Unapproved" state —
                # that's not done, so it counts as failed (same rule web flow uses).
                approved_ok = result.get("approved_ok", je_balanced and approved == "approved")
                run_status = "success" if approved_ok else "failed"
                ok += int(approved_ok)
                failed += int(not approved_ok)
                log.info("est %s (%s) → %s (diff=%.2f, approved=%s)",
                         est_id, name, run_status, je_difference, approved)
                server._record_run(est_id, name, target_date, run_status,
                                   je_difference=je_difference,
                                   je_balanced=je_balanced,
                                   attachment_status=result.get("attachment_status", "skipped"),
                                   approved=approved,
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
