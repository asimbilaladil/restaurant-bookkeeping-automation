"""Duplicate-prevention (live R365 check) and cross-process locking."""

from datetime import date

import pytest

import reconcile_cli as cli
from reconcile_lock import ReconcileLock, LockBusyError, is_locked


# ─── locate_existing_je ──────────────────────────────────────────────────────

def test_locate_existing_je_unverifiable_when_api_not_configured(monkeypatch):
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "")
    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "unverifiable"
    assert "not configured" in result.detail


def test_locate_existing_je_not_found_when_no_je_exists(monkeypatch):
    """No JE at all (e.g. DSS import lag) must block -- there is nothing to
    open or fill, unlike the old (wrong) 'clear = safe to post' model."""
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: [{"id": "loc1", "name": "LCF Airtex"}])
    monkeypatch.setattr(cli.r365_api, "find_location_id", lambda name, locs=None: "loc1")
    monkeypatch.setattr(cli.r365_api, "find_dss_je_matches", lambda loc_id, d: [])

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "not_found"
    assert result.safe_to_update is False


def test_locate_existing_je_ready_when_single_unapproved_match(monkeypatch):
    """A single Unapproved match is the NORMAL, expected, safe-to-update
    state -- this is the core Phase 4 correction."""
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: [{"id": "loc1", "name": "LCF Airtex"}])
    monkeypatch.setattr(cli.r365_api, "find_location_id", lambda name, locs=None: "loc1")
    fake_je = {"number": "NJ00026924", "status": "Unapproved", "debitTotal": 4649.04, "creditTotal": 4649.04}
    monkeypatch.setattr(cli.r365_api, "find_dss_je_matches", lambda loc_id, d: [fake_je])

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "ready"
    assert result.safe_to_update is True
    assert result.je is fake_je


def test_locate_existing_je_already_approved_blocks(monkeypatch):
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: [{"id": "loc1", "name": "LCF Airtex"}])
    monkeypatch.setattr(cli.r365_api, "find_location_id", lambda name, locs=None: "loc1")
    fake_je = {"number": "JE-123", "status": "Approved", "debitTotal": 100.0, "creditTotal": 100.0}
    monkeypatch.setattr(cli.r365_api, "find_dss_je_matches", lambda loc_id, d: [fake_je])

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "already_approved"
    assert result.safe_to_update is False
    assert "JE-123" in result.detail


def test_locate_existing_je_approved_status_case_insensitive(monkeypatch):
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: [{"id": "loc1", "name": "LCF Airtex"}])
    monkeypatch.setattr(cli.r365_api, "find_location_id", lambda name, locs=None: "loc1")
    fake_je = {"number": "JE-123", "status": "APPROVED", "debitTotal": 100.0, "creditTotal": 100.0}
    monkeypatch.setattr(cli.r365_api, "find_dss_je_matches", lambda loc_id, d: [fake_je])

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "already_approved"


def test_locate_existing_je_ambiguous_when_multiple_matches(monkeypatch):
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: [{"id": "loc1", "name": "LCF Airtex"}])
    monkeypatch.setattr(cli.r365_api, "find_location_id", lambda name, locs=None: "loc1")
    fake_jes = [
        {"number": "JE-A", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1},
        {"number": "JE-B", "status": "Unapproved", "debitTotal": 2, "creditTotal": 2},
    ]
    monkeypatch.setattr(cli.r365_api, "find_dss_je_matches", lambda loc_id, d: fake_jes)

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "ambiguous"
    assert result.match_count == 2
    assert result.safe_to_update is False
    assert "JE-A" in result.detail and "JE-B" in result.detail


def test_locate_existing_je_location_not_found_is_unverifiable(monkeypatch):
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: [])
    monkeypatch.setattr(cli.r365_api, "find_location_id", lambda name, locs=None: None)

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "unverifiable"


def test_locate_existing_je_api_exception_is_unverifiable_not_ready(monkeypatch):
    """An API error must never be silently treated as 'safe to update' —
    that would be exactly the unsafe assumption the task forbids."""
    monkeypatch.setattr(cli.r365_api, "BASE_URL", "https://api.example.test")
    monkeypatch.setattr(cli.r365_api, "TOKEN", "tok")
    monkeypatch.setattr(cli.r365_api, "list_locations", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    result = cli.locate_existing_je(32, date(2026, 10, 25), "LCF Airtex")
    assert result.status == "unverifiable"
    assert "boom" in result.detail


# ─── run command: existing JE is normal; only already_approved/ambiguous/
#     not_found/unverifiable block, and none of those have a bypass ────────

def test_run_proceeds_when_single_unapproved_je_found(monkeypatch):
    """Core Phase 4 regression guard: finding exactly one Unapproved JE must
    NOT block the run -- it should proceed to fetch Revel data."""
    import argparse
    fake_je = {"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    fetch_called = []
    def fake_fetch(*a, **k):
        fetch_called.append(1)
        return [{"establishment_id": 6, "data": {
            "sales_data": {}, "product_mix_data": [], "tax_data": [], "discounts_data": [],
        }, "pdf_path": None}]
    monkeypatch.setattr(cli, "fetch_reports", fake_fetch)

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=True)
    rc = cli.cmd_run(args)

    assert fetch_called == [1]  # did reach the fetch step
    assert rc == 0


def test_run_blocks_when_je_already_approved(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("already_approved", detail="JE-999 is Approved"))
    fetch_called = []
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: fetch_called.append(1))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert fetch_called == []  # never even tried to fetch Revel data
    assert "BLOCKED" in capsys.readouterr().out


def test_run_blocks_when_ambiguous(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ambiguous", match_count=2, detail="2 matches"))
    fetch_called = []
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: fetch_called.append(1))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert fetch_called == []
    assert "BLOCKED" in capsys.readouterr().out


def test_run_blocks_when_not_found(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("not_found", detail="no JE yet"))
    fetch_called = []
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: fetch_called.append(1))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert fetch_called == []
    assert "BLOCKED" in capsys.readouterr().out


def test_run_argparse_has_no_force_flag():
    parser = cli.build_parser()
    run_parser = parser._subparsers._group_actions[0].choices["run"]
    option_strings = {opt for action in run_parser._actions for opt in action.option_strings}
    assert not any("force" in o.lower() for o in option_strings)


# ─── ReconcileLock concurrency ───────────────────────────────────────────────

def test_lock_can_be_acquired_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "LOCKS_DIR", tmp_path, raising=False)
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with ReconcileLock(32, date(2026, 10, 25), source="test"):
        assert is_locked(32, date(2026, 10, 25)) is True
    assert is_locked(32, date(2026, 10, 25)) is False


def test_lock_second_acquisition_raises_lock_busy(tmp_path, monkeypatch):
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    lock1 = ReconcileLock(32, date(2026, 10, 25), source="web")
    lock1.__enter__()
    try:
        lock2 = ReconcileLock(32, date(2026, 10, 25), source="cli")
        with pytest.raises(LockBusyError) as exc_info:
            lock2.__enter__()
        assert "32" in str(exc_info.value)
        assert "web" in str(exc_info.value)  # holder info surfaced
    finally:
        lock1.__exit__(None, None, None)


def test_lock_different_dates_do_not_conflict(tmp_path, monkeypatch):
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with ReconcileLock(32, date(2026, 10, 25), source="a"):
        with ReconcileLock(32, date(2026, 10, 26), source="b"):
            pass  # no exception -> different dates don't contend


def test_lock_different_locations_do_not_conflict(tmp_path, monkeypatch):
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with ReconcileLock(32, date(2026, 10, 25), source="a"):
        with ReconcileLock(14, date(2026, 10, 25), source="b"):
            pass


def test_lock_released_after_exception_inside_with_block(tmp_path, monkeypatch):
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with pytest.raises(ValueError):
        with ReconcileLock(32, date(2026, 10, 25), source="a"):
            raise ValueError("simulated crash mid-reconciliation")

    # Lock must be released even though the body raised — otherwise an
    # interrupted run would permanently wedge that location/date.
    assert is_locked(32, date(2026, 10, 25)) is False


def test_run_surfaces_lock_busy_as_blocked_not_a_crash(monkeypatch, capsys):
    import argparse
    monkeypatch.setattr(cli, "locate_existing_je", lambda *a, **k: cli.JELocateResult(
        "ready", je={"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports",
                         lambda *a, **k: [{"establishment_id": 6, "data": {"sales_data": {}}, "pdf_path": None}])

    class _BusyLock:
        def __init__(self, *a, **k): pass
        def __enter__(self):
            raise LockBusyError(6, date(2026, 10, 25), holder="pid=1 source=web")
        def __exit__(self, *a): return False

    monkeypatch.setattr(cli, "R365BrowserLock", _BusyLock)

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert "BLOCKED" in capsys.readouterr().out
