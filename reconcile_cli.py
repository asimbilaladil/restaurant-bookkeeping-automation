#!/usr/bin/env python3
"""
reconcile_cli.py
================
Terminal entrypoint for Daily Sales Reconciliation — runs Revel fetch + R365
journal-entry posting without the Flask frontend, for use over SSH/cron/etc.

This file does NOT reimplement any accounting logic. It imports and calls the
exact same functions the web app uses:

    server._extract_revel_values   - Revel JSON -> JE field mapping (incl.
                                      cash-refund netting, tax-exempt sign
                                      handling, discount buckets, ...)
    server.diagnose_je_imbalance   - why an unbalanced JE didn't tie
    server._record_run             - write a row to dss_runs.db
    server._entity_log             - per-location log file
    revel.fetch_reports            - Revel POS scrape
    r365.open_r365_journal_entry   - R365 browser automation (fill + approve)
    r365.api_client.get_je_for_date - READ-ONLY R365 Public API duplicate check
    db.*                           - the existing SQLite audit history

Importing `server` triggers the same load_dotenv()/db.init_db()/logging setup
the Flask app does — this is the same pattern daily_reconcile.py already uses
to reuse server.py's helpers without running Flask.

Commands:
    reconcile_cli.py run --date 2026-10-25 --all [--dry-run]
    reconcile_cli.py run --date yesterday --location "LCF Beaumont"
    reconcile_cli.py status [--date 2026-10-25] [--location "LCF Beaumont"]
    reconcile_cli.py configure-accounts [--env-file .env]
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import db
import env_manager
import log_safety
import r365_profile_tools
import server  # noqa: F401  (import side effect: load_dotenv + db.init_db + logging)
from r365 import open_r365_journal_entry
from r365 import api_client as r365_api
from r365 import journal_entry as je_journal_entry
from r365.session import PROFILE_DIR as R365_PROFILE_DIR
from revel import fetch_reports, DEFAULT_ESTABLISHMENTS, ESTABLISHMENT_NAMES, R365_NAME_OVERRIDES
from reconcile_lock import R365BrowserLock, LockBusyError, BrowserLockBusyError

ENV_BACKUP_DIR = "env_backups"  # relative to this file's directory; gitignored
R365_PROFILE_BACKUP_DIR = "r365_profile_backups"  # relative to the .env file's directory; gitignored

# A JE whose live R365 status (case-insensitive) matches this is considered
# already approved — never touched again by this tool, in any mode.
_APPROVED_STATUS_VALUES = {"approved"}

# revel_values field -> (R365 account label used by _reconcile() in
# r365/journal_entry.py, side). Confirmed directly against the actual
# `_reconcile(...)` call sites in fill_journal_entry, NOT the function's
# (partly stale) docstring — e.g. the docstring claims employee_tips posts as
# a credit, but the real code calls `_reconcile("2301 - Employee Tips
# Payable", "debit", ...)`. Fields that are computed but never actually
# written by the automation (undeposited_funds, promotions, app_reward,
# revel_discounts_total) are deliberately excluded here rather than guessed.
REVEL_FIELD_TO_ACCOUNT: dict[str, tuple[str, str]] = {
    "food_sales":          ("4000-01", "credit"),
    "beverage_sales":      ("4000-02", "credit"),
    "delivery_food_sales": ("4000-08", "credit"),
    "other_sales":         ("4000-07", "credit"),
    "sales_tax":           ("2240-000", "credit"),
    "credit_card_fees":    ("70250", "credit"),
    "credit_cards_ar":     ("1200-000", "debit"),
    "uber_eats":           ("1245-12", "debit"),
    "doordash":            ("1245-03", "debit"),
    "grubhub":             ("1245-08", "debit"),
    "comps":               ("4500-02", "debit"),
    "employee_discount":   ("5000-17", "debit"),
    "item_discounts":      ("4500-01", "debit"),
    "employee_tips":       ("2301", "debit"),
    # cash_over_short (8000-06) and tax_exempt_amount (4000-011) are
    # sign-dependent (side comes from *_sign / *_field in revel_values) and
    # are handled specially in build_je_comparison() below instead of here.
}

CLI_LOGS_DIR = Path(__file__).parent / "logs" / "cli"  # gitignored, like logs/


class _RedactingLogFilter(logging.Filter):
    """Sanitizes every log record before it's written to the CLI log file.

    `import server` configures root logging via logging.basicConfig(), which
    every module's log.info(...)/log.warning(...) call (revel/operations.py,
    r365/journal_entry.py, r365/session.py, ...) feeds into. Those messages
    can legitimately need to describe a failed network request, and (per
    log_safety.py) a Playwright network-timeout message can embed the full
    request including Cookie/Authorization headers and a session id_token.
    This filter runs log_safety.sanitize_text() over every record so that
    even a library log line we didn't explicitly wrap with safe_exc() can
    never leak a secret into the file on disk.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = log_safety.sanitize_text(str(record.msg))
        args = record.args
        if isinstance(args, Mapping):
            # logging stores a single dict argument (e.g. log.info("%s", {...})
            # or "%(key)s"-style formatting) as the dict itself, not wrapped in
            # a tuple. Iterating it with `for a in args` walks its *keys*,
            # silently turning the dict into a tuple of key names and breaking
            # that record's % formatting later (TypeError: not all arguments
            # converted). Rebuild it as a dict, sanitizing string values only.
            record.args = {
                k: (log_safety.sanitize_text(v) if isinstance(v, str) else v)
                for k, v in args.items()
            }
        elif args:
            # Only sanitize string args — coercing a %d/%f arg to str() breaks
            # that record's % formatting (e.g. "%d bytes", 12345), which
            # raises inside logging's emit() and silently drops the whole
            # line via handleError() instead of writing it to the file.
            record.args = tuple(
                log_safety.sanitize_text(a) if isinstance(a, str) else a
                for a in args
            )
        return True


def _configure_cli_logging() -> Path:
    """Redirect the verbose, library-level logging (Revel fetch internals,
    R365 browser navigation, every `log.info()` in revel/*.py and r365/*.py)
    to a timestamped file under logs/cli/, instead of the terminal.

    Why this exists: `import server` (needed to reuse server._extract_revel_values,
    server._record_run, etc. — see the module docstring) runs
    logging.basicConfig(...) as a side effect, which attaches a console
    StreamHandler to the root logger. Every module-level `log = logging.
    getLogger(__name__)` in this codebase then prints through that handler,
    so a single `run` could emit hundreds of INFO lines (cookie pruning,
    frame detection, row-by-row R365 grid reads, ...) interleaved with the
    CLI's own short, meaningful summary — exactly the "excessive terminal
    logging" this function fixes.

    The CLI's own plain print() calls in cmd_run/cmd_status/etc. are
    untouched by this function and remain the only thing printed to the
    terminal; this function only affects the `logging` module's output.
    Full detail (every log.info/log.warning/log.error from every module) is
    preserved in the file, redacted via _RedactingLogFilter, for
    troubleshooting.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)

    CLI_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = CLI_LOGS_DIR / f"reconcile_{ts}.log"

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    file_handler.addFilter(_RedactingLogFilter())
    root.addHandler(file_handler)
    root.setLevel(logging.INFO)

    return log_path


# ─── Date / location parsing ────────────────────────────────────────────────

def parse_date_arg(raw: str) -> date:
    """Accept YYYY-MM-DD, 'today', or 'yesterday' (all in UTC, matching the
    rest of the app — server.py's SSE routes and daily_reconcile.py both
    reason about "the date being reconciled" in UTC terms)."""
    low = raw.strip().lower()
    today = datetime.now(timezone.utc).date()
    if low == "today":
        return today
    if low == "yesterday":
        return today - timedelta(days=1)
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"invalid date '{raw}' — use YYYY-MM-DD, 'today', or 'yesterday'"
        )


def resolve_locations(all_flag: bool, location_args: list[str] | None) -> list[int]:
    """--all -> every DEFAULT_ESTABLISHMENTS id.
    --location NAME (repeatable, case-insensitive exact match on the display
    name, e.g. "LCF Beaumont") -> just those ids, in the order given.
    Raises ValueError (caller turns this into a clean CLI error) if neither
    or both are given, or a name doesn't match."""
    if all_flag and location_args:
        raise ValueError("pass either --all or --location, not both")
    if not all_flag and not location_args:
        raise ValueError("pass --all or at least one --location")
    if all_flag:
        return list(DEFAULT_ESTABLISHMENTS)

    by_lower = {name.lower(): est_id for est_id, name in ESTABLISHMENT_NAMES.items()}
    ids = []
    for raw in location_args:
        key = raw.strip().lower()
        if key not in by_lower:
            available = ", ".join(sorted(ESTABLISHMENT_NAMES.values()))
            raise ValueError(f"unknown location '{raw}'. Available: {available}")
        ids.append(by_lower[key])
    return ids


# ─── Secret-safe helpers ─────────────────────────────────────────────────────

def redact(_value: str) -> str:
    """Never used to print a real secret — exists so call sites are explicit
    about intent and a reviewer can grep for any place a secret is logged."""
    return "********"


# ─── Locating the existing DSS journal entry ────────────────────────────────
#
# IMPORTANT architecture correction (Phase 4): R365 auto-generates exactly one
# DSS journal-entry placeholder per (location, date) as soon as that day's
# entity exists in the DSS grid. The intended workflow has ALWAYS been to
# locate that existing JE, fill its accounting rows from Revel, attach the
# Revel report, and approve it — see r365/journal_entry.py's
# go_to_daily_sales_summary(), which navigates to the location's existing
# "Journal Entry" tab (never creates a new one) before calling
# fill_journal_entry(). Finding an existing JE is therefore the NORMAL,
# REQUIRED precondition for every run, not evidence of a duplicate.
#
# Phase 2.1 got this backwards: it treated "a JE already exists" as reason to
# block. The actual risks worth guarding against are narrower:
#   - the JE is already Approved (must never be touched again)
#   - more than one DSS-marker JE matches this location/date (ambiguous —
#     which one is "the" target is no longer obvious)
#   - no JE exists yet at all (R365 hasn't generated the DSS entity for this
#     date yet, e.g. DSS import lag — there is nothing to open/fill)
#   - the live API can't be reached/configured (can't verify any of the above)
# Only a single, non-approved match is safe to proceed with.

class JELocateResult:
    def __init__(self, status: str, je: dict | None = None, match_count: int = 0, detail: str = ""):
        # status: "ready" | "already_approved" | "ambiguous" | "not_found" | "unverifiable"
        self.status = status
        self.je = je
        self.match_count = match_count
        self.detail = detail

    @property
    def safe_to_update(self) -> bool:
        return self.status == "ready"


def locate_existing_je(establishment_id: int, target_date: date, r365_name: str) -> JELocateResult:
    """Query the R365 Public API (read-only) to find and classify the single
    intended DSS journal entry for this location/date — the entity the real
    automation is always supposed to open, fill, and approve.

    This is independent of dss_runs.db on purpose: SQLite only records what
    THIS tool believes happened on its own last attempt. It cannot see a JE
    created or approved by a different process (the web UI, the old orphaned
    process, or a human in R365 directly). The Public API's
    /accounting/v1/transactions endpoint is the independent source of truth —
    the same read-side source the R365 Public API integration (commit
    3c2c58b) and fill_journal_entry()'s own read-side lookup already use.

    Fail-closed: any state other than exactly one non-approved match blocks a
    LIVE run, with no override flag anywhere in this tool.
    """
    if not r365_api.BASE_URL or not r365_api.TOKEN:
        return JELocateResult(
            "unverifiable",
            detail="R365 Public API is not configured (R365_API_BASE_URL/R365_API_TOKEN "
                   "missing) — cannot locate or verify the target JE.",
        )
    try:
        locations = r365_api.list_locations()
        loc_id = r365_api.find_location_id(r365_name, locations)
        if not loc_id:
            return JELocateResult(
                "unverifiable",
                detail=f"R365 API has no location matching '{r365_name}' — cannot check.",
            )
        matches = r365_api.find_dss_je_matches(loc_id, target_date)
    except Exception as exc:
        return JELocateResult("unverifiable", detail=f"R365 API check failed: {log_safety.safe_exc(exc)}")

    if not matches:
        return JELocateResult(
            "not_found",
            detail=f"No DSS journal entry found yet for {r365_name} on {target_date} — "
                   "R365 may not have generated the DSS entity for this date yet "
                   "(known import lag of >28h in some cases). Nothing to open or fill.",
        )
    if len(matches) > 1:
        numbers = ", ".join(m.get("number", "?") for m in matches)
        return JELocateResult(
            "ambiguous", match_count=len(matches),
            detail=f"{len(matches)} DSS journal entries match {r365_name} on {target_date}: "
                   f"{numbers}. Cannot determine a single unambiguous target — resolve "
                   "manually in R365 first.",
        )

    je = matches[0]
    status = (je.get("status") or "").strip().lower()
    if status in _APPROVED_STATUS_VALUES:
        return JELocateResult(
            "already_approved", je=je, match_count=1,
            detail=f"JE {je.get('number')} for {r365_name} on {target_date} is already "
                   f"Approved (debit={je.get('debitTotal')}, credit={je.get('creditTotal')}). "
                   "Never modified or re-approved by this tool.",
        )
    return JELocateResult(
        "ready", je=je, match_count=1,
        detail=f"JE {je.get('number')} for {r365_name} on {target_date} — status="
               f"{je.get('status')}, debit={je.get('debitTotal')}, credit={je.get('creditTotal')}.",
    )


def build_je_comparison(revel_values: dict, existing_je: dict | None) -> list[dict]:
    """Compare computed revel_values against the existing R365 JE's current
    booked line amounts, account by account. Read-only — never writes
    anything. Returns a list of {field, account, side, revel, booked, match}
    rows, in a stable, reportable order. Only fields this automation actually
    writes (see REVEL_FIELD_TO_ACCOUNT) plus the two sign-dependent special
    cases (cash_over_short, tax_exempt_amount) are included."""
    lines = r365_api.je_lines_by_account(existing_je) if existing_je else {}
    rows = []

    def _booked(account: str, side: str) -> float | None:
        # Reuses r365.journal_entry._api_lookup — the SAME disambiguation
        # logic the real fill path already relies on. A naive lines.get(account)
        # here would silently pick whichever same-side row for this account
        # happens to appear first in the API response, which for an account
        # like "4500-01 - Discounts" can be a named sub-row (e.g. "Gift Card
        # Redeemed") instead of the plain row the automation actually targets
        # — producing a fabricated discrepancy that isn't real. _api_lookup
        # already prefers the no-comment row when more than one non-zero
        # candidate exists, exactly matching journal_entry.py's own behavior.
        val = je_journal_entry._api_lookup(lines, account, side)
        return round(val, 2) if val is not None else None

    for field, (account, side) in REVEL_FIELD_TO_ACCOUNT.items():
        if field not in revel_values:
            continue
        revel_val = round(float(revel_values.get(field) or 0), 2)
        booked_val = _booked(account, side)
        rows.append({
            "field": field, "account": account, "side": side,
            "revel": revel_val, "booked": booked_val,
            "match": booked_val is not None and booked_val == revel_val,
        })

    # Sign-dependent special cases.
    cos = round(float(revel_values.get("cash_over_short") or 0), 2)
    cos_sign = revel_values.get("cash_over_short_sign", "credit")
    if cos:
        booked_val = _booked("8000-06", cos_sign)
        rows.append({
            "field": "cash_over_short", "account": "8000-06", "side": cos_sign,
            "revel": cos, "booked": booked_val, "match": booked_val is not None and booked_val == cos,
        })

    tax_exempt = round(float(revel_values.get("tax_exempt_amount") or 0), 2)
    tax_exempt_field = revel_values.get("tax_exempt_field")
    if tax_exempt and tax_exempt_field:
        booked_val = _booked("4000-011", tax_exempt_field)
        rows.append({
            "field": "tax_exempt_amount", "account": "4000-011", "side": tax_exempt_field,
            "revel": tax_exempt, "booked": booked_val,
            "match": booked_val is not None and booked_val == tax_exempt,
        })

    return rows


def print_je_comparison(rows: list[dict]) -> None:
    if not rows:
        print("  [comparison] No existing JE line data available to compare against.")
        return
    print(f"  [comparison] Revel-computed vs. currently-booked R365 values "
          f"({sum(1 for r in rows if not r['match'])} of {len(rows)} differ):")
    for r in rows:
        booked_s = "—" if r["booked"] is None else f"{r['booked']:.2f}"
        marker = "=" if r["match"] else "≠"
        print(f"    {r['account']:10s} ({r['side']:6s}) {r['field']:20s} "
              f"revel={r['revel']:>10.2f} {marker} booked={booked_s:>10s}")


# ─── run ─────────────────────────────────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> int:
    try:
        target_date = parse_date_arg(args.date)
        est_ids = resolve_locations(args.all, args.location)
    except (argparse.ArgumentTypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if target_date > datetime.now(timezone.utc).date():
        print("error: --date cannot be in the future", file=sys.stderr)
        return 2

    # getattr(..., False): tolerates a hand-built argparse.Namespace (as used
    # throughout the test suite) that predates this flag.
    update_only = getattr(args, "update_only", False)
    if update_only and args.dry_run:
        print("error: --update-only and --dry-run are mutually exclusive", file=sys.stderr)
        return 2

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"=== Reconciliation [{mode}] — date={target_date} — "
          f"{len(est_ids)} location(s) ===")

    exit_code = 0
    for est_id in est_ids:
        name = ESTABLISHMENT_NAMES.get(est_id, str(est_id))
        r365_name = R365_NAME_OVERRIDES.get(est_id, name)
        print(f"\n--- {name} (est {est_id}) ---")

        # 1. Prior SQLite success is informative but NEVER authoritative on
        #    its own (see locate_existing_je docstring) — still worth telling
        #    the operator up front so a normal "already done" case is obvious
        #    without waiting for the API round-trip.
        prior_success = db.count_runs(run_date=str(target_date), establishment_id=est_id, status="success")
        if prior_success:
            print(f"  [info] {prior_success} prior successful run recorded in dss_runs.db for this date.")

        # 2. Locate + classify the single intended existing DSS JE via the
        #    live R365 API (fail-closed). Only "ready" (exactly one match,
        #    not already Approved) allows a LIVE run to proceed — there is no
        #    confirmation prompt and no flag anywhere in this tool that lets
        #    an operator bypass "already_approved" or "ambiguous". SQLite
        #    history is never accepted as a substitute for this check.
        #    (Dry-run is read-only and has no R365 side effects either way,
        #    so it still proceeds for visibility even when unverifiable — it
        #    reports the ambiguity but never posts anything.)
        locate = locate_existing_je(est_id, target_date, r365_name)
        if locate.status == "already_approved":
            print(f"  [BLOCKED] {locate.detail}")
            exit_code = 1
            if not args.dry_run:
                continue
        elif locate.status == "ambiguous":
            print(f"  [BLOCKED] {locate.detail}")
            exit_code = 1
            if not args.dry_run:
                continue
        elif locate.status == "not_found":
            print(f"  [BLOCKED] {locate.detail}")
            exit_code = 1
            if not args.dry_run:
                continue
        elif locate.status == "unverifiable":
            print(f"  [BLOCKED] {locate.detail}")
            if args.dry_run:
                print("  [dry-run] Continuing read-only for visibility — "
                      "a LIVE run would be blocked here with no override.")
            else:
                print("  Refusing to post — cannot independently locate/verify the "
                      "target JE, and this tool never treats local run history as a "
                      "substitute. Fix the R365 API configuration "
                      "(R365_API_BASE_URL/R365_API_TOKEN) or verify manually in R365.")
                exit_code = 1
                continue
        else:  # "ready"
            print(f"  [ok] {locate.detail}")

        if update_only and locate.status != "ready":
            # update-only is strictly opt-in and even more conservative than
            # a normal run: it must never proceed on anything but a single,
            # verified, non-approved target.
            print("  [BLOCKED] --update-only requires a verified 'ready' target JE.")
            exit_code = 1
            continue

        # 3. Fetch Revel data for this one location. Progress narrative
        #    ("Fetching...", "OK") is intentionally not printed here -- it
        #    added terminal noise without information; folded into the final
        #    one-line summary below instead. Full step-by-step detail from
        #    revel/operations.py's own logging still goes to the CLI log file.
        try:
            results = fetch_reports(target_date, [est_id])
        except Exception as exc:
            # safe_exc(), not str(exc): see log_safety.py -- a Playwright
            # network-timeout message can embed the full request, including
            # Cookie/Authorization headers and a session id_token. Confirmed
            # live during Phase 4 testing; never print/store the raw text.
            safe_msg = log_safety.safe_exc(exc)
            print(f"  [error] Revel fetch failed: {safe_msg}")
            if not args.dry_run:
                server._record_run(est_id, name, target_date, "error", error=safe_msg)
            exit_code = 1
            continue

        result = results[0] if results else {}
        if result.get("error") or not result.get("data"):
            msg = result.get("error") or "no data returned"
            print(f"  [error] Revel: {msg}")
            if not args.dry_run:
                server._record_run(est_id, name, target_date, "error", error=msg)
            exit_code = 1
            continue

        # 4. Same mapping function the web app and the cron job use —
        #    no accounting logic is duplicated here.
        revel_values = server._extract_revel_values(result["data"])
        pdf_path = result.get("pdf_path")

        if args.dry_run:
            _print_dry_run_summary(name, target_date, result["data"], revel_values)
            # Task-requested read-only comparison against whatever the API
            # could locate (if anything) — still zero R365 side effects.
            print_je_comparison(build_je_comparison(revel_values, locate.je))
            continue

        if update_only:
            # --update-only: fill/save accounting rows on the existing JE
            # ONLY. Never attach (pdf_path is simply never passed), never
            # approve, never create anything. See open_r365_journal_entry's
            # update_only parameter in r365/journal_entry.py.
            before_je = locate.je
            print(f"  [update-only] Target JE {before_je.get('number')} — "
                  f"before: debit={before_je.get('debitTotal')} credit={before_je.get('creditTotal')} "
                  f"status={before_je.get('status')}")
            try:
                with R365BrowserLock(est_id, target_date, source="cli-update-only"):
                    with server._entity_log(name, target_date) as log_path:
                        print("  Updating R365 accounting rows only (no attachment, no approve)...")
                        je_result = open_r365_journal_entry(
                            target_date, r365_name, revel_values,
                            attachment_path=None, update_only=True,
                        )
            except (BrowserLockBusyError, LockBusyError) as exc:
                print(f"  [BLOCKED] {exc}")
                exit_code = 1
                continue

            if "error" in je_result:
                print(f"  [error] R365: {je_result['error']}")
                exit_code = 1
                continue

            print(f"  [update-only] Save complete. attachment_status={je_result.get('attachment_status')} "
                  f"approved={je_result.get('approved')} je_difference={je_result.get('je_difference')}")
            if je_result.get("attachment_status") not in ("skipped_update_only",) or \
               je_result.get("approved") not in ("skipped_update_only",):
                print("  [WARNING] update-only mode did not skip attach/approve as expected — "
                      "investigate before trusting this run.")

            # Re-read via the read-only API and report exactly what changed.
            after_locate = locate_existing_je(est_id, target_date, r365_name)
            if after_locate.je:
                after_je = after_locate.je
                print(f"  [update-only] Re-verified after update — JE {after_je.get('number')} "
                      f"status={after_je.get('status')} (expect still Unapproved) "
                      f"debit={after_je.get('debitTotal')} credit={after_je.get('creditTotal')}")
                if (after_je.get("status") or "").strip().lower() in _APPROVED_STATUS_VALUES:
                    print("  [CRITICAL] JE shows Approved after an update-only run — "
                          "this must never happen; investigate immediately.")
                before_lines = r365_api.je_lines_by_account(before_je)
                after_lines = r365_api.je_lines_by_account(after_je)
                all_accounts = set(before_lines) | set(after_lines)
                changed_accounts = sorted(a for a in all_accounts if before_lines.get(a) != after_lines.get(a))
                print(f"  [update-only] Exact changes made ({len(changed_accounts)} account line(s) differ):")
                for acct in changed_accounts:
                    b = before_lines.get(acct, {})
                    a = after_lines.get(acct, {})
                    print(f"    {acct}: debit {b.get('debit', 0.0):.2f} -> {a.get('debit', 0.0):.2f}, "
                          f"credit {b.get('credit', 0.0):.2f} -> {a.get('credit', 0.0):.2f}")
            else:
                print(f"  [WARNING] Could not re-verify JE after update: {after_locate.detail}")
            continue

        # 5. Live post (full fill + attach + approve) — guarded by
        #    R365BrowserLock, which composes:
        #      (a) the GLOBAL browser lock — only one process anywhere may
        #          be mid-session against the shared Playwright profile
        #          (shared by JE posting AND the report-viewer export), and
        #      (b) the per-(establishment, date) ReconcileLock — preserved
        #          from Phase 2, giving a precise "this location/date" error.
        #    The web frontend's /api/r365/navigate, /api/r365/reconcile-all,
        #    and /api/r365/report-viewer routes acquire the same locks, so
        #    the CLI and the web UI can never both be mid-browser-session.
        try:
            with R365BrowserLock(est_id, target_date, source="cli"):
                with server._entity_log(name, target_date) as log_path:
                    # Posting progress ("headless browser... this can take a
                    # few minutes") moved to the CLI log file -- the detailed
                    # per-step narrative from r365/journal_entry.py's own
                    # logging already lands there via _configure_cli_logging().
                    je_result = open_r365_journal_entry(target_date, r365_name, revel_values, pdf_path)
        except BrowserLockBusyError as exc:
            print(f"  [BLOCKED] {exc}")
            exit_code = 1
            continue
        except LockBusyError as exc:
            print(f"  [BLOCKED] {exc}")
            exit_code = 1
            continue

        if "error" in je_result:
            print(f"  [error] R365: {je_result['error']}")
            server._record_run(est_id, name, target_date, "error",
                                error=je_result["error"], log_filename=log_path.name)
            exit_code = 1
            continue

        je_balanced = je_result.get("je_balanced", True)
        je_difference = je_result.get("je_difference", 0.0)
        approved = je_result.get("approved", "skipped")
        approved_ok = je_result.get("approved_ok", je_balanced and approved == "approved")
        run_status = "success" if approved_ok else "failed"

        diagnosis = None
        if not je_balanced:
            diagnosis = server.diagnose_je_imbalance(result["data"], revel_values, je_difference)

        server._record_run(
            est_id, name, target_date, run_status,
            je_difference=je_difference, je_balanced=je_balanced,
            attachment_status=je_result.get("attachment_status", "skipped"),
            approved=approved, log_filename=log_path.name, diagnosis=diagnosis,
        )

        # One concise, complete summary line per the requested fields:
        # location, date, Revel fetched, JE updated, balanced/unbalanced,
        # attachment status, approval status, final success/failure.
        je_number = (locate.je or {}).get("number", "?")
        balanced_s = f"yes (diff={je_difference:.2f})" if je_balanced else f"NO (diff={je_difference:.2f})"
        result_s = "SUCCESS" if run_status == "success" else "FAILED"
        print(f"  {name} | {target_date} | Revel: OK | JE {je_number}: updated | "
              f"balanced: {balanced_s} | attachment: {je_result.get('attachment_status', 'skipped')} | "
              f"approved: {approved} | {result_s}")
        if run_status != "success":
            if diagnosis and diagnosis.get("explained"):
                print(f"    cause: {diagnosis['headline']} "
                      f"({diagnosis['revel_label']}={diagnosis['revel_value']} vs "
                      f"{diagnosis['booked_label']}={diagnosis['booked_value']})")
            exit_code = 1

    print(f"\n=== Done [{mode}] ===")
    return exit_code


def _print_dry_run_summary(name: str, target_date: date, data: dict, revel_values: dict) -> None:
    """Dry-run output: show exactly what WOULD be posted, with zero R365 side
    effects — no browser launch, no lock acquired, no row written to
    dss_runs.db. The frontend's corresponding behaviour (there isn't one —
    the web UI has no dry-run mode) makes this CLI-only."""
    print(f"  [dry-run] Would post JE for {name} on {target_date}:")
    fields = [
        "food_sales", "beverage_sales", "delivery_food_sales", "other_sales",
        "tax_exempt_amount", "tax_exempt_field", "sales_tax", "credit_cards_ar",
        "uber_eats", "doordash", "grubhub", "undeposited_funds", "cash_refunds",
        "comps", "item_discounts", "employee_discount", "employee_tips",
        "cash_over_short", "cash_over_short_sign",
    ]
    for f in fields:
        if f in revel_values:
            print(f"    {f:22s} = {revel_values[f]}")
    # Reuse the same discrepancy logic the live path would only run on an
    # actual imbalance — here we can't know je_difference (R365 never saw
    # it), but we can still show the Revel-vs-booked cross-check inputs.
    print("  [dry-run] No browser session opened. No lock acquired. No DB row written.")


# ─── status ──────────────────────────────────────────────────────────────────

def cmd_status(args: argparse.Namespace) -> int:
    run_date = str(parse_date_arg(args.date)) if args.date else None
    est_id = None
    if args.location:
        try:
            est_id = resolve_locations(False, [args.location])[0]
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    runs = db.get_runs(limit=args.limit, run_date=run_date, establishment_id=est_id)
    if not runs:
        print("No matching runs recorded.")
        return 0

    print(f"{'created_at (UTC)':20s} {'location':22s} {'date':10s} {'status':8s} "
          f"{'je_diff':>9s} {'approved':10s}")
    print("-" * 85)
    for r in runs:
        loc = r.get("establishment_name") or ESTABLISHMENT_NAMES.get(r.get("establishment_id"), "?")
        diff = r.get("je_difference")
        diff_s = f"{diff:.2f}" if diff is not None else "—"
        print(f"{r.get('created_at',''):20s} {loc:22s} {r.get('run_date',''):10s} "
              f"{r.get('status',''):8s} {diff_s:>9s} {r.get('approved') or '—':10s}")

    failed = db.get_failed_no_success(run_date)
    if failed:
        print(f"\n{len(failed)} location/date pair(s) failed with no later success:")
        for r in failed:
            loc = r.get("establishment_name") or ESTABLISHMENT_NAMES.get(r.get("establishment_id"), "?")
            print(f"  {loc} — {r.get('run_date')} — {r.get('status')}: {r.get('error') or ''}")
    return 0


# ─── configure-accounts ──────────────────────────────────────────────────────

def _prompt_credential_pair(label: str, user_key: str, pass_key: str,
                             current_user: str | None, current_pass_set: bool) -> dict[str, str]:
    """Prompt for one account's email + password. Blank input on either field
    means "keep the existing value" (the explicit retain mechanism) — a
    genuinely empty credential can never be *set*, only ever *kept*."""
    print(f"\n{label}")
    if current_user:
        print(f"  Current email: {current_user}")
    else:
        print("  Current email: (not set)")
    print(f"  Current password: {'(set)' if current_pass_set else '(not set)'}")

    new_email = input("  New email (leave blank to keep current): ").strip()
    new_pass = getpass.getpass("  New password (leave blank to keep current): ")

    updates: dict[str, str] = {}
    if new_email:
        updates[user_key] = new_email
    elif not current_user:
        print(f"  [warning] no email currently set for {user_key} and none entered — leaving unset.")

    if new_pass:
        updates[pass_key] = new_pass
    # No "confirm password" re-prompt by design: getpass gives no visual
    # feedback, so a mistyped confirmation is just as likely to be wrong as
    # the original — operators can re-run this command immediately if unsure.

    return updates


def cmd_configure_accounts(args: argparse.Namespace) -> int:
    env_path = args.env_file
    lines = env_manager.read_lines(env_path)

    print("=== Configure Revel / R365 account credentials ===")
    print(f"Target file: {env_path}")
    print("Note: this updates browser-LOGIN credentials only (REVEL_USER/PASS, "
          "R65_USER/PASS). It does NOT touch R365_API_TOKEN/TENANT_ID/SECURITY_ID "
          "(the Public API credentials) — those are rotated separately, never here.")

    do_revel = input("\nUpdate Revel credentials? [y/N]: ").strip().lower() == "y"
    do_r365 = input("Update R365 (browser login) credentials? [y/N]: ").strip().lower() == "y"

    if not do_revel and not do_r365:
        print("\nNo changes requested. .env left untouched.")
        return 0

    updates: dict[str, str] = {}
    if do_revel:
        updates.update(_prompt_credential_pair(
            "Revel account", "REVEL_USER", "REVEL_PASS",
            env_manager.get_value(lines, "REVEL_USER"),
            env_manager.get_value(lines, "REVEL_PASS") is not None,
        ))
    if do_r365:
        updates.update(_prompt_credential_pair(
            "R365 account (browser login)", "R65_USER", "R65_PASS",
            env_manager.get_value(lines, "R65_USER"),
            env_manager.get_value(lines, "R65_PASS") is not None,
        ))

    if not updates:
        print("\nAll fields left blank — nothing to change. .env left untouched.")
        return 0

    print(f"\nAbout to update: {', '.join(sorted(updates.keys()))}")
    confirm = input("Write these changes to the .env file now? [y/N]: ").strip().lower()
    if confirm != "y":
        print("Aborted. No changes written.")
        return 1

    from pathlib import Path
    backup_dir = Path(env_path).resolve().parent / ENV_BACKUP_DIR
    backup_path = env_manager.backup_file(env_path, backup_dir)
    print(f"Backed up current .env to {backup_path} (permissions 600).")

    appended = env_manager.set_values(env_path, updates)
    import os
    os.chmod(env_path, 0o600)

    for key in sorted(updates.keys()):
        verb = "added" if key in appended else "updated"
        print(f"  ✓ {key} {verb} (value not shown)")

    # R365 browser-login credentials changed -> the persistent Chromium
    # profile may still hold a cached, logged-in session for the OLD
    # account. Back it up (never delete) and rename it aside so the next
    # browser launch is forced to log in fresh with whatever is now in
    # .env. Scoped strictly to an actual R65_USER/R65_PASS change — a
    # Revel-only update must never touch the R365 profile.
    if "R65_USER" in updates or "R65_PASS" in updates:
        profile_dir = getattr(args, "profile_dir", None) or R365_PROFILE_DIR
        profile_backup_dir = Path(env_path).resolve().parent / R365_PROFILE_BACKUP_DIR
        outcome = r365_profile_tools.rotate_profile(profile_dir, profile_backup_dir)
        if outcome["had_existing_profile"]:
            print(f"\nR365 browser profile backed up to {outcome['backup_path']} (permissions 600).")
            print(f"Old profile invalidated (renamed, not deleted) to {outcome['invalidated_path']}.")
            print("Next R365 browser login will start a fresh session using the new credentials.")
        else:
            print("\nNo existing R365 browser profile found yet — nothing to back up or invalidate.")

    print("\n.env updated atomically. Restart the service for the new "
          "credentials to take effect on the next login attempt "
          "(not done automatically by this command).")
    return 0


# ─── argparse wiring ─────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reconcile_cli.py",
        description="Terminal Daily Sales Reconciliation (Revel -> R365), no frontend required.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Fetch Revel + post/approve R365 journal entries")
    run_p.add_argument("--date", required=True, help="YYYY-MM-DD, 'today', or 'yesterday'")
    group = run_p.add_mutually_exclusive_group(required=True)
    group.add_argument("--all", action="store_true", help="All configured locations")
    group.add_argument("--location", action="append",
                        help='Location name, e.g. "LCF Beaumont" (repeatable)')
    run_p.add_argument("--dry-run", action="store_true",
                        help="Compute and print JE values; never post, approve, or write a DB row")
    run_p.add_argument("--update-only", action="store_true",
                        help="Update ONLY the existing JE's accounting rows: never attach, "
                             "never approve, never create. Requires a verified single "
                             "non-approved target JE; mutually exclusive with --dry-run.")
    run_p.set_defaults(func=cmd_run)

    status_p = sub.add_parser("status", help="Show recent reconciliation run history")
    status_p.add_argument("--date", help="Filter to YYYY-MM-DD / 'today' / 'yesterday'")
    status_p.add_argument("--location", help="Filter to one location name")
    status_p.add_argument("--limit", type=int, default=50)
    status_p.set_defaults(func=cmd_status)

    cfg_p = sub.add_parser("configure-accounts", help="Interactively update Revel/R365 login credentials")
    cfg_p.add_argument("--env-file", default=".env", help="Path to the .env file to update (default: .env)")
    cfg_p.add_argument("--profile-dir", default=None,
                        help=argparse.SUPPRESS)  # test/advanced override of the R365 browser profile path
    cfg_p.set_defaults(func=cmd_configure_accounts)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log_path = _configure_cli_logging()
    if args.command == "run":
        print(f"(detailed logs: {log_path})")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
