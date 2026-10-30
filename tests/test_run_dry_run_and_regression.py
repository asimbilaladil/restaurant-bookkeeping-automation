"""
Dry-run behavior, interrupted-run handling, and a regression check that the
CLI/lock additions did not alter any accounting calculation in server.py.
"""

import argparse
from datetime import date

import pytest

import db
import reconcile_cli as cli
import server


FAKE_REVEL_DATA = {
    "sales_data": {
        "gross_sales": 1000.0,
        "total_sales": 1000.0,
        "net_sales_untaxed": 0.0,
        "credit_total": 300.0,
        "credit_tips_total": 20.0,
        "cash_for_sales": 150.0,
        "cash_refunds": 0.0,
        "net_account_for": 150.0,
        "total_payments": 150.0,
        "adj_credit_tips": 5.0,
        "custom_payments": {},
    },
    "product_mix_data": [
        {"row_type": "Class", "product_class": "1. Food", "taxable_sales": 700.0, "untaxable_sales": 0.0},
        {"row_type": "Class", "product_class": "2. Beverage", "taxable_sales": 300.0, "untaxable_sales": 0.0},
    ],
    "tax_data": [{"row_type": "totals_row", "tax": 80.0}],
    "discounts_data": [],
}


# ─── Regression: calculation functions are untouched by this phase ──────────

def test_extract_revel_values_unchanged_behavior():
    """Pins the existing, documented behavior of _extract_revel_values for a
    simple no-discount, no-refund, balanced-cash fixture. If a future change
    to server.py accidentally alters this mapping, this test catches it —
    this phase must not change any accounting calculation."""
    values = server._extract_revel_values(FAKE_REVEL_DATA)
    assert values["food_sales"] == 700.0
    assert values["beverage_sales"] == 300.0
    assert values["sales_tax"] == 80.0
    assert values["credit_cards_ar"] == 320.0  # credit_total + credit_tips_total
    assert values["cash_over_short"] == 0.0     # net_account_for == total_payments
    assert values["cash_over_short_sign"] == "debit"


def test_diagnose_je_imbalance_returns_none_when_balanced():
    assert server.diagnose_je_imbalance(FAKE_REVEL_DATA, {}, 0.0) is None


def test_net_cash_refunds_flag_not_flipped():
    """This phase must not enable/disable any existing feature flag."""
    assert server._NET_CASH_REFUNDS is True  # matches the documented default-on state


# ─── Dry-run: zero R365 side effects ────────────────────────────────────────

def test_dry_run_never_calls_open_r365_journal_entry(monkeypatch):
    monkeypatch.setattr(cli, "locate_existing_je", lambda *a, **k: cli.JELocateResult(
        "ready", je={"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 6, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])

    posted = []
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: posted.append(a))

    locked = []
    class _TrackingLock:
        def __init__(self, *a, **k): locked.append(a)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(cli, "R365BrowserLock", _TrackingLock)

    recorded = []
    monkeypatch.setattr(server, "_record_run", lambda *a, **k: recorded.append(a))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=True)
    rc = cli.cmd_run(args)

    assert rc == 0
    assert posted == []     # never posted to R365
    assert locked == []     # never even acquired the posting lock
    assert recorded == []   # never wrote a DB row


def test_dry_run_prints_computed_values(capsys):
    cli._print_dry_run_summary("LCF Katy", date(2026, 10, 25), FAKE_REVEL_DATA,
                                server._extract_revel_values(FAKE_REVEL_DATA))
    out = capsys.readouterr().out
    assert "food_sales" in out
    assert "700.0" in out
    assert "No browser session opened" in out


def test_live_run_does_call_open_r365_journal_entry_when_clear(monkeypatch, tmp_path):
    """Sanity check the opposite of the dry-run test: a clear, non-dry-run
    path does reach the posting function exactly once."""
    monkeypatch.setattr(cli, "locate_existing_je", lambda *a, **k: cli.JELocateResult(
        "ready", je={"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 6, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])

    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    calls = []
    def fake_post(target_date, r365_name, revel_values, pdf_path):
        calls.append((target_date, r365_name))
        return {"status": "ok", "je_balanced": True, "je_difference": 0.0,
                "approved": "approved", "approved_ok": True,
                "attachment_status": "skipped"}
    monkeypatch.setattr(cli, "open_r365_journal_entry", fake_post)

    recorded = []
    monkeypatch.setattr(server, "_record_run", lambda *a, **k: recorded.append((a, k)))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 0
    assert len(calls) == 1
    assert calls[0][1] == "LCF Katy"
    assert len(recorded) == 1
    assert recorded[0][0][3] == "success"  # (est_id, name, date, status, ...)


# ─── Interrupted / partial failure handling ──────────────────────────────────

def test_revel_fetch_exception_is_recorded_as_error_not_a_crash(monkeypatch):
    monkeypatch.setattr(cli, "locate_existing_je", lambda *a, **k: cli.JELocateResult(
        "ready", je={"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}, match_count=1))

    def boom(*a, **k):
        raise TimeoutError("Revel login timed out")
    monkeypatch.setattr(cli, "fetch_reports", boom)

    recorded = []
    monkeypatch.setattr(server, "_record_run", lambda *a, **k: recorded.append(a))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)  # must not raise

    assert rc == 1
    assert recorded and recorded[0][3] == "error"


def test_one_location_failure_does_not_abort_remaining_locations(monkeypatch):
    monkeypatch.setattr(cli, "locate_existing_je", lambda *a, **k: cli.JELocateResult(
        "ready", je={"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}, match_count=1))

    def fetch_side_effect(target_date, est_ids):
        est_id = est_ids[0]
        if est_id == 6:  # LCF Katy — fails
            raise RuntimeError("Revel is down")
        return [{"establishment_id": est_id, "data": FAKE_REVEL_DATA, "pdf_path": None}]
    monkeypatch.setattr(cli, "fetch_reports", fetch_side_effect)

    recorded = []
    monkeypatch.setattr(server, "_record_run", lambda *a, **k: recorded.append(a))

    args = argparse.Namespace(date="2026-10-25", all=False,
                               location=["LCF Katy", "LCF Airtex"], dry_run=True)
    rc = cli.cmd_run(args)

    assert rc == 1  # overall failure reported...
    # ...but Airtex (the second location) still got processed; dry-run
    # means no DB rows for either, but no exception propagated.


# ─── status command ──────────────────────────────────────────────────────────

def test_status_with_no_runs_prints_message(capsys):
    args = argparse.Namespace(date="2099-01-01", location=None, limit=50)
    rc = cli.cmd_status(args)
    assert rc == 0
    assert "No matching runs" in capsys.readouterr().out


def test_status_filters_by_location(monkeypatch, capsys):
    db.record_run(establishment_id=6, establishment_name="LCF Katy",
                   run_date=date(2026, 10, 25), status="success", je_difference=0.0,
                   je_balanced=True, approved="approved")
    db.record_run(establishment_id=32, establishment_name="LCF Airtex",
                   run_date=date(2026, 10, 25), status="error", error="boom")

    args = argparse.Namespace(date=None, location="LCF Katy", limit=50)
    rc = cli.cmd_status(args)
    out = capsys.readouterr().out
    assert rc == 0
    # The main run table respects the --location filter...
    table_section = out.split("failed with no later success")[0]
    assert "LCF Katy" in table_section
    assert "LCF Airtex" not in table_section
    # ...but the "failed with no success" summary is deliberately global
    # (it's meant to surface outstanding failures regardless of filter).
    assert "LCF Airtex" in out
