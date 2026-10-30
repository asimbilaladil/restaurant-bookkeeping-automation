"""
Phase 4: corrected existing-JE workflow and --update-only safeguards.
"""

import argparse
from datetime import date

import pytest

import reconcile_cli as cli
import server


FAKE_REVEL_DATA = {
    "sales_data": {
        "gross_sales": 1000.0, "total_sales": 1000.0, "net_sales_untaxed": 0.0,
        "credit_total": 300.0, "credit_tips_total": 20.0, "cash_for_sales": 150.0,
        "cash_refunds": 0.0, "net_account_for": 150.0, "total_payments": 150.0,
        "adj_credit_tips": 5.0, "custom_payments": {},
    },
    "product_mix_data": [
        {"row_type": "Class", "product_class": "1. Food", "taxable_sales": 700.0, "untaxable_sales": 0.0},
        {"row_type": "Class", "product_class": "2. Beverage", "taxable_sales": 300.0, "untaxable_sales": 0.0},
    ],
    "tax_data": [{"row_type": "totals_row", "tax": 80.0}],
    "discounts_data": [],
}


# ─── build_je_comparison ──────────────────────────────────────────────────────

def test_build_je_comparison_matches_identical_values():
    revel_values = {"food_sales": 700.0, "beverage_sales": 300.0}
    existing_je = {"details": [
        {"glAccount": {"number": "4000-01"}, "debit": 0.0, "credit": 700.0, "comment": ""},
        {"glAccount": {"number": "4000-02"}, "debit": 0.0, "credit": 300.0, "comment": ""},
    ]}
    rows = cli.build_je_comparison(revel_values, existing_je)
    assert all(r["match"] for r in rows)


def test_build_je_comparison_flags_mismatch():
    revel_values = {"food_sales": 700.0}
    existing_je = {"details": [
        {"glAccount": {"number": "4000-01"}, "debit": 0.0, "credit": 650.0, "comment": ""},
    ]}
    rows = cli.build_je_comparison(revel_values, existing_je)
    assert rows[0]["match"] is False
    assert rows[0]["revel"] == 700.0
    assert rows[0]["booked"] == 650.0


def test_build_je_comparison_none_existing_je_shows_booked_none():
    revel_values = {"food_sales": 700.0}
    rows = cli.build_je_comparison(revel_values, None)
    assert rows[0]["booked"] is None
    assert rows[0]["match"] is False


def test_build_je_comparison_excludes_fields_automation_never_writes():
    """undeposited_funds, promotions, etc. are computed but never actually
    written to R365 by fill_journal_entry -- must not appear as a fabricated
    comparison row."""
    revel_values = {"undeposited_funds": 238.1, "promotions": 21.58, "food_sales": 100.0}
    rows = cli.build_je_comparison(revel_values, None)
    fields = {r["field"] for r in rows}
    assert "undeposited_funds" not in fields
    assert "promotions" not in fields
    assert "food_sales" in fields


def test_build_je_comparison_handles_sign_dependent_cash_over_short():
    revel_values = {"cash_over_short": 4.0, "cash_over_short_sign": "credit"}
    existing_je = {"details": [
        {"glAccount": {"number": "8000-06"}, "debit": 0.0, "credit": 4.0, "comment": "Variance"},
    ]}
    rows = cli.build_je_comparison(revel_values, existing_je)
    row = next(r for r in rows if r["field"] == "cash_over_short")
    assert row["match"] is True


def test_print_je_comparison_no_data(capsys):
    cli.print_je_comparison([])
    assert "No existing JE line data" in capsys.readouterr().out


# ─── dry-run includes the comparison, still zero side effects ──────────────

def test_dry_run_prints_comparison_against_located_je(monkeypatch, capsys):
    fake_je = {"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1,
               "details": [{"glAccount": {"number": "4000-01"}, "debit": 0.0, "credit": 700.0, "comment": ""}]}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 6, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    posted = []
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: posted.append(a))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=True)
    rc = cli.cmd_run(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert posted == []
    assert "comparison" in out.lower()
    assert "4000-01" in out


# ─── core Phase 4 correction: existing JE is normal, not a duplicate ───────

def test_run_does_not_treat_unapproved_existing_je_as_block(monkeypatch):
    """This is the exact regression Phase 4 exists to fix."""
    fake_je = {"number": "NJ00026924", "status": "Unapproved", "debitTotal": 4649.04, "creditTotal": 4649.04}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    fetch_called = []
    def fake_fetch(*a, **k):
        fetch_called.append(1)
        return [{"establishment_id": 6, "data": FAKE_REVEL_DATA, "pdf_path": None}]
    monkeypatch.setattr(cli, "fetch_reports", fake_fetch)

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=True)
    rc = cli.cmd_run(args)

    assert fetch_called == [1]
    assert rc == 0


# ─── --update-only: argparse wiring ──────────────────────────────────────────

def test_update_only_flag_parses():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--date", "2026-10-22", "--location", "LCF Cypress", "--update-only"])
    assert args.update_only is True
    assert args.dry_run is False


def test_update_only_and_dry_run_are_mutually_exclusive(capsys):
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--date", "2026-10-22", "--location", "LCF Cypress",
                               "--update-only", "--dry-run"])
    rc = cli.cmd_run(args)
    assert rc == 2
    assert "mutually exclusive" in capsys.readouterr().err


def test_update_only_defaults_false_for_hand_built_namespace():
    """Existing tests build argparse.Namespace by hand without update_only --
    cmd_run must tolerate that (getattr default) rather than raising
    AttributeError. A future date trips the pre-existing early-exit path
    (rc=2) -- reaching that cleanly proves getattr(update_only) didn't blow
    up first."""
    args = argparse.Namespace(date="2099-01-01", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)  # must not raise AttributeError
    assert rc == 2


# ─── --update-only: safety gating ────────────────────────────────────────────

def test_update_only_blocked_when_not_ready(monkeypatch, capsys):
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("not_found", detail="no JE yet"))
    posted = []
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: posted.append(a))

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert posted == []
    assert "BLOCKED" in capsys.readouterr().out


def test_update_only_blocked_when_already_approved(monkeypatch, capsys):
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("already_approved", detail="JE-9 is Approved"))
    posted = []
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: posted.append(a))

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert posted == []


def test_update_only_blocked_when_ambiguous(monkeypatch, capsys):
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ambiguous", match_count=2, detail="2 matches"))
    posted = []
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: posted.append(a))

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert posted == []


def test_update_only_never_passes_attachment_path(monkeypatch, tmp_path):
    """Even if a pdf_path came back from the Revel fetch, update-only must
    never pass it through to open_r365_journal_entry."""
    fake_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": "/tmp/would_be_attached.pdf"}
    ])

    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    calls = []
    def fake_post(target_date, r365_name, revel_values, attachment_path=None, update_only=False):
        calls.append({"attachment_path": attachment_path, "update_only": update_only})
        return {"status": "ok", "je_balanced": True, "je_difference": 0.0,
                "approved": "skipped_update_only", "attachment_status": "skipped_update_only"}
    monkeypatch.setattr(cli, "open_r365_journal_entry", fake_post)
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    rc = cli.cmd_run(args)

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["attachment_path"] is None
    assert calls[0]["update_only"] is True


def test_update_only_never_writes_a_dss_runs_row(monkeypatch, tmp_path):
    """update-only is a controlled numeric correction, not a reconciliation
    run -- it must not pollute dss_runs.db's success/failure history."""
    fake_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "skipped_update_only", "attachment_status": "skipped_update_only",
    })

    recorded = []
    monkeypatch.setattr(server, "_record_run", lambda *a, **k: recorded.append(a))

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    cli.cmd_run(args)

    assert recorded == []


def test_update_only_reports_exact_changes(monkeypatch, tmp_path, capsys):
    before_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 100.0, "creditTotal": 100.0,
                 "details": [{"glAccount": {"number": "4000-01"}, "debit": 0.0, "credit": 90.0, "comment": ""}]}
    after_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 110.0, "creditTotal": 110.0,
                "details": [{"glAccount": {"number": "4000-01"}, "debit": 0.0, "credit": 100.0, "comment": ""}]}

    calls = {"n": 0}
    def fake_locate(*a, **k):
        calls["n"] += 1
        je = before_je if calls["n"] == 1 else after_je
        return cli.JELocateResult("ready", je=je, match_count=1)
    monkeypatch.setattr(cli, "locate_existing_je", fake_locate)
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "skipped_update_only", "attachment_status": "skipped_update_only",
    })

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    rc = cli.cmd_run(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Exact changes made" in out
    assert "4000-01" in out
    assert "90.00 -> 100.00" in out


def test_update_only_critical_warning_if_somehow_approved_after(monkeypatch, tmp_path, capsys):
    """Defense in depth: if the re-read ever shows Approved after an
    update-only run (which should be structurally impossible), scream loudly
    rather than silently reporting success."""
    before_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    after_je = {"number": "TX56", "status": "Approved", "debitTotal": 1, "creditTotal": 1}

    calls = {"n": 0}
    def fake_locate(*a, **k):
        calls["n"] += 1
        je = before_je if calls["n"] == 1 else after_je
        return cli.JELocateResult("ready" if calls["n"] == 1 else "already_approved", je=je, match_count=1)
    monkeypatch.setattr(cli, "locate_existing_je", fake_locate)
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "skipped_update_only", "attachment_status": "skipped_update_only",
    })

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    cli.cmd_run(args)

    assert "CRITICAL" in capsys.readouterr().out


def test_update_only_warns_if_attach_or_approve_not_skipped(monkeypatch, tmp_path, capsys):
    """Defense in depth against a future regression in journal_entry.py that
    stops honoring update_only internally."""
    fake_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "approved",  # should have been "skipped_update_only"
        "attachment_status": "skipped_update_only",
    })

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    cli.cmd_run(args)

    assert "WARNING" in capsys.readouterr().out


def test_update_only_uses_r365_browser_lock(monkeypatch, tmp_path):
    fake_je = {"number": "TX56", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "skipped_update_only", "attachment_status": "skipped_update_only",
    })

    lock_calls = []
    class _TrackingLock:
        def __init__(self, *a, **k): lock_calls.append(a)
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr(cli, "R365BrowserLock", _TrackingLock)

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=True)
    cli.cmd_run(args)

    assert len(lock_calls) == 1


# ─── journal_entry.py: update_only threads through and gates attach/approve ─

def test_fill_journal_entry_update_only_skips_attachment_and_approve():
    """fill_journal_entry() does extensive real DOM/Kendo-grid interaction
    before reaching the attach/approve gates, so a full browser-free unit
    test would require stubbing dozens of Playwright calls out of scope for
    this phase. Instead, assert the structural contract directly: the
    update_only branch must unconditionally set both status sentinels before
    the attachment-upload and Approve-click code paths can run."""
    from r365 import journal_entry as je_mod
    import inspect
    src = inspect.getsource(je_mod.fill_journal_entry)
    assert "if update_only:" in src
    assert 'attachment_status = "skipped_update_only"' in src
    assert 'approved = "skipped_update_only"' in src
    # The guard must appear BEFORE the real upload/approve calls, not after.
    upload_idx = src.index("_upload_attachment(")
    approve_idx = src.index('active.evaluate("""\n                    () => {\n                        const el = document.querySelector(\'[data-testid="approveMenuItem"]\'')
    update_only_attach_idx = src.index('attachment_status = "skipped_update_only"')
    update_only_approve_idx = src.index('approved = "skipped_update_only"')
    assert update_only_attach_idx < upload_idx
    assert update_only_approve_idx < approve_idx


def test_open_r365_journal_entry_has_update_only_param():
    from r365 import journal_entry as je_mod
    import inspect
    sig = inspect.signature(je_mod.open_r365_journal_entry)
    assert "update_only" in sig.parameters
    assert sig.parameters["update_only"].default is False


def test_go_to_daily_sales_summary_forwards_update_only():
    from r365 import journal_entry as je_mod
    import inspect
    src = inspect.getsource(je_mod.go_to_daily_sales_summary)
    assert "update_only=update_only" in src


# ─── Phase 4.1: full mocked-browser integration test of fill_journal_entry ──
#
# Exercises the REAL fill_journal_entry() function end-to-end (not just
# source inspection) against a fake Playwright frame/page, proving that in
# update_only mode: _upload_attachment is never called, the Approve DOM click
# is never reached, and _recreate_journal_entry (which would reset the JE and
# risk the existing attachment) is never called even when a self-heal
# condition is present.

class _FakeLocator:
    def __init__(self, count=1):
        self._count = count
    def count(self):
        return self._count
    @property
    def first(self):
        return self
    def wait_for(self, **kw):
        pass
    def scroll_into_view_if_needed(self):
        pass
    def click(self, **kw):
        pass
    def fill(self, *a, **kw):
        pass
    def press(self, *a, **kw):
        pass
    def filter(self, **kw):
        return self
    def locator(self, *a, **kw):
        return self


class _FakeKeyboard:
    def press(self, *a):
        pass
    def type(self, *a):
        pass


class _FakeFrame:
    def __init__(self):
        self.url = "fake://je-frame"
        self.keyboard = _FakeKeyboard()
    def locator(self, *a, **kw):
        return _FakeLocator()
    def wait_for_timeout(self, *a):
        pass
    def evaluate(self, script="", *a, **kw):
        # fill_journal_entry runs several different inline JS reads against
        # the JE frame; shape the fake return by what the script is asking
        # for rather than trying to track call order.
        if "offsetHeight" in script:
            return True  # frame-visibility check during frame detection
        if "DISCOUNT_ACCOUNTS" in script:
            return []    # discount-rows read -> no extra rows to reconcile
        return None


class _FakeActive:
    def __init__(self, je_frame):
        self.frames = [je_frame]
    def evaluate(self, script, *a, **kw):
        # Used for the Save click (and would be used for Approve, which must
        # never be reached in update_only mode).
        return "clicked"
    def wait_for_timeout(self, *a):
        pass
    def wait_for_function(self, *a, **kw):
        pytest.fail("must never wait for approveMenuItem in update_only mode")
    def expect_response(self, *a, **kw):
        pytest.fail("must never click Approve (expect_response) in update_only mode")
    def screenshot(self, *a, **kw):
        pass


# revel_values with every optional/inline-grid-editing field zeroed out
# (cash_over_short, item_discounts, other_sales, tax_exempt) so the test only
# has to fake the generic _reconcile() read/write path, not every bespoke
# inline Kendo-grid interaction block.
_MINIMAL_REVEL_VALUES = {
    "food_sales": 100.0, "beverage_sales": 0.0, "delivery_food_sales": 0.0,
    "other_sales": 0.0, "sales_tax": 0.0, "credit_card_fees": 0.0,
    "credit_cards_ar": 0.0, "uber_eats": 0.0, "doordash": 0.0, "grubhub": 0.0,
    "comps": 0.0, "employee_discount": 0.0, "employee_tips": 0.0,
    "cash_over_short": 0.0, "cash_over_short_sign": "credit",
    "tax_exempt_field": None, "tax_exempt_amount": 0.0,
}


def test_fill_journal_entry_mocked_browser_update_only_never_attaches_or_approves(monkeypatch):
    from r365 import journal_entry as je_mod

    upload_calls = []
    recreate_calls = []
    monkeypatch.setattr(je_mod, "_upload_attachment",
                         lambda *a, **k: upload_calls.append(a) or pytest.fail("must never upload"))
    monkeypatch.setattr(je_mod, "_recreate_journal_entry",
                         lambda *a, **k: recreate_calls.append(a) or pytest.fail("must never recreate"))
    monkeypatch.setattr(je_mod, "_assign_missing_payment_types", lambda *a, **k: [])
    monkeypatch.setattr(je_mod, "_read_je_cell_value", lambda frame, account, field, api_lines=None: 0.0)
    monkeypatch.setattr(je_mod, "_fill_je_cell", lambda *a, **k: None)
    monkeypatch.setattr(je_mod, "_screenshot_je_grid", lambda frame, path: 0.0)  # balanced

    je_frame = _FakeFrame()
    active = _FakeActive(je_frame)

    je_diff, attachment_status, approved = je_mod.fill_journal_entry(
        active, _MINIMAL_REVEL_VALUES,
        attachment_path="/tmp/would_be_attached.pdf",  # deliberately non-None
        location_name=None, target_date=None,
        update_only=True,
    )

    assert attachment_status == "skipped_update_only"
    assert approved == "skipped_update_only"
    assert upload_calls == []
    assert recreate_calls == []


def test_fill_journal_entry_mocked_browser_update_only_skips_stale_tax_exempt_recreate(monkeypatch):
    """The self-heal path specifically: a stale 4000-011 row would normally
    trigger _recreate_journal_entry(). In update_only mode it must not."""
    from r365 import journal_entry as je_mod

    recreate_calls = []
    monkeypatch.setattr(je_mod, "_recreate_journal_entry",
                         lambda *a, **k: recreate_calls.append(a) or pytest.fail("must never recreate"))
    monkeypatch.setattr(je_mod, "_upload_attachment", lambda *a, **k: pytest.fail("must never upload"))
    monkeypatch.setattr(je_mod, "_assign_missing_payment_types", lambda *a, **k: [])
    monkeypatch.setattr(je_mod, "_fill_je_cell", lambda *a, **k: None)
    monkeypatch.setattr(je_mod, "_screenshot_je_grid", lambda frame, path: 0.0)

    # Simulate a stale 4000-011 row: the "wrong" side has a nonzero value.
    def fake_read(frame, account, field, api_lines=None):
        if account == "4000-011" and field == "credit":  # the "wrong" field for heal_field="debit"
            return 42.0
        return 0.0
    monkeypatch.setattr(je_mod, "_read_je_cell_value", fake_read)

    revel_values = dict(_MINIMAL_REVEL_VALUES)
    revel_values["tax_exempt_field"] = "debit"
    revel_values["tax_exempt_amount"] = 10.0

    je_frame = _FakeFrame()
    active = _FakeActive(je_frame)

    je_diff, attachment_status, approved = je_mod.fill_journal_entry(
        active, revel_values, attachment_path=None,
        location_name=None, target_date=None, update_only=True,
    )

    assert recreate_calls == []
    assert attachment_status == "skipped_update_only"
    assert approved == "skipped_update_only"


def test_fill_journal_entry_mocked_browser_non_update_only_does_attach_and_approve(monkeypatch):
    """Sanity check the opposite: a normal (non-update-only) balanced run DOES
    reach attachment upload and the Approve click -- proves the mocked
    harness itself is valid and update_only is what's gating behavior above,
    not a broken fake."""
    from r365 import journal_entry as je_mod

    upload_calls = []
    monkeypatch.setattr(je_mod, "_upload_attachment", lambda *a, **k: upload_calls.append(a) or "uploaded")
    monkeypatch.setattr(je_mod, "_recreate_journal_entry", lambda *a, **k: None)
    monkeypatch.setattr(je_mod, "_assign_missing_payment_types", lambda *a, **k: [])
    monkeypatch.setattr(je_mod, "_read_je_cell_value", lambda frame, account, field, api_lines=None: 0.0)
    monkeypatch.setattr(je_mod, "_fill_je_cell", lambda *a, **k: None)
    monkeypatch.setattr(je_mod, "_screenshot_je_grid", lambda frame, path: 0.0)

    approve_reached = {"approve_evaluate": False}

    class _ApprovingActive(_FakeActive):
        def evaluate(self, script, *a, **kw):
            if "approveMenuItem" in script:
                approve_reached["approve_evaluate"] = True
                return "clicked"
            return "clicked"
        def wait_for_function(self, *a, **kw):
            pass  # allow the approve wait in this non-update-only test
        def expect_response(self, *a, **kw):
            class _Ctx:
                def __enter__(self_inner): return self_inner
                def __exit__(self_inner, *a): return False
                value = type("R", (), {"ok": True, "status": 200})()
            return _Ctx()

    je_frame = _FakeFrame()
    active = _ApprovingActive(je_frame)

    je_diff, attachment_status, approved = je_mod.fill_journal_entry(
        active, _MINIMAL_REVEL_VALUES, attachment_path="/tmp/would_be_attached.pdf",
        location_name=None, target_date=None, update_only=False,
    )

    assert upload_calls == [("/tmp/would_be_attached.pdf",)] or len(upload_calls) == 1
    assert approve_reached["approve_evaluate"] is True
    assert approved == "approved"
