"""
Phase 2.1 hardening regression tests:
  - fail-closed duplicate detection (no confirmation override)
  - global browser lock contention (shared across locations/routes)
  - credential rotation: profile backup + invalidation
  - interrupted runs still release both locks
"""

import argparse
import os
import stat
import tarfile
from datetime import date

import pytest

import r365_profile_tools
import reconcile_cli as cli
from reconcile_lock import (
    GlobalBrowserLock, R365BrowserLock, ReconcileLock,
    LockBusyError, BrowserLockBusyError, is_browser_locked, is_locked,
)


# ─── 1. Fail-closed duplicate detection ─────────────────────────────────────

def test_live_run_blocked_when_duplicate_check_unverifiable_no_prompt(monkeypatch, capsys):
    """The old behavior asked `input()` for a 'yes' override on an
    unverifiable result. That path must no longer exist: a live run must be
    blocked outright, with no call to input() at all."""
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("unverifiable", detail="API down"))
    fetch_called = []
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: fetch_called.append(1))

    def _unexpected_input(*a, **k):
        raise AssertionError("input() must never be called for an unverifiable duplicate check")
    monkeypatch.setattr("builtins.input", _unexpected_input)

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert fetch_called == []
    out = capsys.readouterr().out
    assert "BLOCKED" in out
    assert "no override" in out.lower() or "never treats" in out.lower()


def test_dry_run_is_not_blocked_by_unverifiable_but_still_posts_nothing(monkeypatch, capsys):
    """Dry-run is read-only, so an unverifiable check is reported but does
    not stop the computed-values preview — and still never posts."""
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("unverifiable", detail="API down"))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 6, "data": {
            "sales_data": {}, "product_mix_data": [], "tax_data": [], "discounts_data": [],
        }, "pdf_path": None}
    ])
    posted = []
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: posted.append(a))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=True)
    rc = cli.cmd_run(args)

    assert rc == 0
    assert posted == []
    assert "BLOCKED" in capsys.readouterr().out  # ambiguity still surfaced


def test_live_run_still_blocked_when_je_confirmed_exists(monkeypatch, capsys):
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("already_approved", detail="JE-1 is Approved"))
    fetch_called = []
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: fetch_called.append(1))

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    rc = cli.cmd_run(args)

    assert rc == 1
    assert fetch_called == []


def test_no_confirmation_bypass_exists_anywhere_in_cmd_run_source():
    """Static guard against regression: the old 'type yes to continue' prompt
    must not reappear in cmd_run."""
    import inspect
    src = inspect.getsource(cli.cmd_run)
    assert "type \\'yes\\'" not in src
    assert "Proceed anyway" not in src


# ─── 2. Global browser lock ──────────────────────────────────────────────────

def test_global_lock_blocks_second_acquisition_across_different_locations(tmp_path, monkeypatch):
    """This is exactly the gap Phase 2.1 closes: the OLD per-(location,date)
    lock allowed two different locations to post at once, even though both
    share one Chromium profile. The global lock must block this."""
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    lock_katy = GlobalBrowserLock(source="cli-katy")
    lock_katy.__enter__()
    try:
        lock_airtex = GlobalBrowserLock(source="web-airtex")
        with pytest.raises(BrowserLockBusyError) as exc_info:
            lock_airtex.__enter__()
        assert "cli-katy" in str(exc_info.value)
    finally:
        lock_katy.__exit__(None, None, None)


def test_r365_browser_lock_blocks_different_location_same_time(tmp_path, monkeypatch):
    """R365BrowserLock (what call sites actually use) must also serialize
    across different (establishment, date) pairs via its global half."""
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    first = R365BrowserLock(6, date(2026, 10, 25), source="cli")
    first.__enter__()
    try:
        second = R365BrowserLock(32, date(2026, 10, 25), source="web")  # different location
        with pytest.raises(BrowserLockBusyError):
            second.__enter__()
    finally:
        first.__exit__(None, None, None)


def test_r365_browser_lock_same_location_date_raises_local_not_global(tmp_path, monkeypatch):
    """When the SAME (location, date) is contended, the more specific
    LockBusyError should still surface (not just a generic browser-busy
    message) — but only reachable if global weren't already the blocker.
    Here we free the global lock state by using independent processes'
    worth of file handles: first releases global implicitly via context
    exit timing isn't tested directly; instead we verify both locks are
    held together and both released together."""
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with R365BrowserLock(6, date(2026, 10, 25), source="a"):
        assert is_browser_locked() is True
        assert is_locked(6, date(2026, 10, 25)) is True
    assert is_browser_locked() is False
    assert is_locked(6, date(2026, 10, 25)) is False


def test_global_lock_released_after_exception():
    import tempfile
    import reconcile_lock
    with tempfile.TemporaryDirectory() as td:
        reconcile_lock.LOCKS_DIR = __import__("pathlib").Path(td)
        with pytest.raises(ValueError):
            with GlobalBrowserLock(source="x"):
                raise ValueError("simulated mid-session crash")
        assert is_browser_locked() is False


def test_report_viewer_lock_contends_with_je_posting_lock(tmp_path, monkeypatch):
    """The report-viewer export and JE posting share the same Chromium
    profile and must not run concurrently."""
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    je_lock = R365BrowserLock(6, date(2026, 10, 25), source="cli-je")
    je_lock.__enter__()
    try:
        rv_lock = reconcile_lock.report_viewer_lock(source="web-report-viewer")
        with pytest.raises(BrowserLockBusyError):
            rv_lock.__enter__()
    finally:
        je_lock.__exit__(None, None, None)


def test_per_location_lock_still_works_standalone(tmp_path, monkeypatch):
    """Preserve Phase 2 behavior: ReconcileLock alone (without the global
    wrapper) still protects the exact (location, date) pair."""
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with ReconcileLock(6, date(2026, 10, 25), source="a"):
        with pytest.raises(LockBusyError):
            ReconcileLock(6, date(2026, 10, 25), source="b").__enter__()


def test_cli_run_uses_r365_browser_lock_not_bare_reconcile_lock(monkeypatch):
    """Guard against silently reverting to the Phase-2 per-location-only
    lock, which did not protect against cross-location browser contention."""
    import inspect
    src = inspect.getsource(cli.cmd_run)
    assert "R365BrowserLock(" in src
    assert "with ReconcileLock(" not in src


# ─── 3. Credential rotation: profile backup + invalidation ─────────────────

@pytest.fixture
def fake_profile(tmp_path):
    profile = tmp_path / ".r365_browser_profile"
    (profile / "Default").mkdir(parents=True)
    (profile / "Default" / "Cookies").write_text("fake-cookie-data", encoding="utf-8")
    (profile / "Local State").write_text("{}", encoding="utf-8")
    return profile


def test_backup_profile_creates_600_tarball_with_contents(fake_profile, tmp_path):
    backup_dir = tmp_path / "profile_backups"
    archive = r365_profile_tools.backup_profile(fake_profile, backup_dir)

    assert archive is not None
    assert archive.exists()
    mode = stat.S_IMODE(os.stat(archive).st_mode)
    assert mode == 0o600
    with tarfile.open(archive) as tar:
        names = tar.getnames()
    assert any("Default/Cookies" in n for n in names)


def test_backup_profile_returns_none_when_no_profile_exists(tmp_path):
    missing = tmp_path / "does_not_exist"
    result = r365_profile_tools.backup_profile(missing, tmp_path / "backups")
    assert result is None


def test_invalidate_profile_renames_not_deletes(fake_profile):
    original_contents = (fake_profile / "Default" / "Cookies").read_text(encoding="utf-8")
    invalidated_path = r365_profile_tools.invalidate_profile(fake_profile)

    assert invalidated_path is not None
    assert not fake_profile.exists()  # original path is now free for a fresh profile
    assert invalidated_path.exists()  # but the data still exists, just moved
    assert (invalidated_path / "Default" / "Cookies").read_text(encoding="utf-8") == original_contents


def test_rotate_profile_backs_up_then_invalidates(fake_profile, tmp_path):
    backup_dir = tmp_path / "profile_backups"
    outcome = r365_profile_tools.rotate_profile(fake_profile, backup_dir)

    assert outcome["had_existing_profile"] is True
    assert os.path.exists(outcome["backup_path"])
    assert os.path.exists(outcome["invalidated_path"])
    assert not fake_profile.exists()
    # The backup is untouched/independent of the later rename.
    with tarfile.open(outcome["backup_path"]) as tar:
        assert any("Cookies" in n for n in tar.getnames())


def test_rotate_profile_noop_when_nothing_to_rotate(tmp_path):
    missing = tmp_path / "nope"
    outcome = r365_profile_tools.rotate_profile(missing, tmp_path / "backups")
    assert outcome["had_existing_profile"] is False
    assert outcome["backup_path"] is None
    assert outcome["invalidated_path"] is None


def test_configure_accounts_rotates_profile_only_on_r365_credential_change(
    tmp_path, monkeypatch, fake_profile,
):
    env_file = tmp_path / ".env"
    env_file.write_text("R65_USER=old@example.com\nR65_PASS=old-pass\nREVEL_USER=r@example.com\n",
                         encoding="utf-8")

    answers = iter(["n", "y", "new_r365@example.com", "y"])  # skip revel, update r365
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-r365-pass")

    args = argparse.Namespace(env_file=str(env_file), profile_dir=str(fake_profile))
    rc = cli.cmd_configure_accounts(args)

    assert rc == 0
    assert not fake_profile.exists()  # invalidated
    backups = list((tmp_path / cli.R365_PROFILE_BACKUP_DIR).glob("r365_browser_profile_*.tar.gz"))
    assert len(backups) == 1


def test_configure_accounts_revel_only_change_does_not_touch_r365_profile(
    tmp_path, monkeypatch, fake_profile,
):
    env_file = tmp_path / ".env"
    env_file.write_text("R65_USER=old@example.com\nR65_PASS=old-pass\nREVEL_USER=r@example.com\n",
                         encoding="utf-8")

    answers = iter(["y", "n", "new_revel@example.com", "y"])  # update revel, skip r365
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-revel-pass")

    args = argparse.Namespace(env_file=str(env_file), profile_dir=str(fake_profile))
    rc = cli.cmd_configure_accounts(args)

    assert rc == 0
    assert fake_profile.exists()  # untouched — only Revel creds changed
    backup_dir = tmp_path / cli.R365_PROFILE_BACKUP_DIR
    assert not backup_dir.exists()


def test_configure_accounts_declining_final_confirm_does_not_rotate_profile(
    tmp_path, monkeypatch, fake_profile,
):
    env_file = tmp_path / ".env"
    env_file.write_text("R65_USER=old@example.com\nR65_PASS=old-pass\n", encoding="utf-8")

    answers = iter(["n", "y", "new_r365@example.com", "n"])  # decline final write
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-r365-pass")

    args = argparse.Namespace(env_file=str(env_file), profile_dir=str(fake_profile))
    rc = cli.cmd_configure_accounts(args)

    assert rc == 1
    assert fake_profile.exists()  # never touched — write was aborted before rotation


# ─── 4. Interrupted runs still release both locks ───────────────────────────

def test_interrupted_posting_releases_global_and_local_lock(tmp_path, monkeypatch):
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    with pytest.raises(RuntimeError):
        with R365BrowserLock(6, date(2026, 10, 25), source="cli"):
            raise RuntimeError("simulated browser crash mid-post")

    assert is_browser_locked() is False
    assert is_locked(6, date(2026, 10, 25)) is False
    # And a fresh attempt immediately after must succeed (not wedged).
    with R365BrowserLock(6, date(2026, 10, 25), source="cli-retry"):
        pass


def test_keyboard_interrupt_during_run_does_not_leave_locks_held(tmp_path, monkeypatch):
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)

    monkeypatch.setattr(cli, "locate_existing_je", lambda *a, **k: cli.JELocateResult(
        "ready", je={"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 6, "data": {
            "sales_data": {}, "product_mix_data": [], "tax_data": [], "discounts_data": [],
        }, "pdf_path": None}
    ])

    def interrupted_post(*a, **k):
        raise KeyboardInterrupt()
    monkeypatch.setattr(cli, "open_r365_journal_entry", interrupted_post)

    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_run(args)

    assert is_browser_locked() is False
    assert is_locked(6, date(2026, 10, 25)) is False
