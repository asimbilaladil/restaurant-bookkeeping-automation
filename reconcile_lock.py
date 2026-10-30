"""
reconcile_lock.py
==================
Cross-process locking so the terminal CLI and the web frontend can never
corrupt or race each other's R365 browser automation.

Two separate locks, used together:

1. GlobalBrowserLock — ONE lock for the entire process tree, covering every
   code path that calls Playwright's launch_persistent_context() against the
   shared profile at r365/session.py:PROFILE_DIR (~/.r365_browser_profile).
   That profile dir is shared by BOTH open_r365_journal_entry()
   (r365/journal_entry.py) and open_report_viewer() (r365/report_viewer.py),
   and is reachable from FOUR independent entry points:
     - server.py  /api/r365/navigate          (single JE)
     - server.py  /api/r365/reconcile-all      (JE loop, one location at a time)
     - server.py  /api/r365/report-viewer      (Excel export)
     - reconcile_cli.py  run                   (CLI JE posting)
   Two of these launching launch_persistent_context() on the SAME profile
   directory at the same time is not a benign race: Chromium's own
   SingletonLock inside a user-data-dir means the second launch either fails
   outright or, worse, can corrupt the profile's session state for both.
   The OLD per-(establishment, date) lock alone does NOT prevent this,
   because it allows two *different* locations (or a JE post running
   alongside a report-viewer export) to both reach launch_persistent_context()
   concurrently from separate processes. GlobalBrowserLock closes that gap.

2. ReconcileLock — the existing per-(establishment_id, date) lock, preserved
   unchanged. It still provides a clear, specific "this exact location/date
   is already being reconciled" message, which GlobalBrowserLock's generic
   "the browser is busy" message does not convey on its own. Call sites
   should hold GlobalBrowserLock for the whole browser session and
   ReconcileLock for the specific (location, date) being posted, in that
   order (global outer, per-location inner) — see R365BrowserLock below,
   which does exactly that as a single composed context manager.

Both locks are non-blocking advisory fcntl.flock() locks on files under
locks/ — they raise immediately rather than waiting, since reconciliation
runs take minutes and a silent wait would hide a real concurrency problem
from the operator instead of surfacing it.
"""

from __future__ import annotations

import errno
import fcntl
import os
import socket
from datetime import date
from pathlib import Path

LOCKS_DIR = Path(__file__).parent / "locks"


class LockBusyError(RuntimeError):
    """Raised when another process already holds the per-(location, date) lock."""

    def __init__(self, establishment_id, target_date, holder: str | None = None):
        self.establishment_id = establishment_id
        self.target_date = target_date
        self.holder = holder
        msg = (
            f"Reconciliation already in progress for establishment={establishment_id} "
            f"date={target_date}"
        )
        if holder:
            msg += f" (held by: {holder})"
        super().__init__(msg)


class BrowserLockBusyError(RuntimeError):
    """Raised when another process already holds the GLOBAL R365 browser lock
    (i.e. is mid-session against the shared persistent Chromium profile)."""

    def __init__(self, holder: str | None = None):
        self.holder = holder
        msg = "R365 browser automation is already running in another process (shared profile is busy)"
        if holder:
            msg += f" (held by: {holder})"
        super().__init__(msg)


class _FlockFile:
    """Shared non-blocking flock primitive. Not used directly — see
    ReconcileLock / GlobalBrowserLock below."""

    def __init__(self, path: Path, source: str, busy_exc_factory):
        self._path = path
        self._source = source
        self._busy_exc_factory = busy_exc_factory
        self._fh = None

    def acquire(self) -> None:
        LOCKS_DIR.mkdir(exist_ok=True)
        self._fh = open(self._path, "a+")
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._fh.close()
            self._fh = None
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                holder = None
                try:
                    holder = self._path.read_text(encoding="utf-8").strip() or None
                except OSError:
                    pass
                raise self._busy_exc_factory(holder) from None
            raise

        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(f"pid={os.getpid()} host={socket.gethostname()} source={self._source}\n")
        self._fh.flush()

    def release(self) -> None:
        if self._fh is not None:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            finally:
                self._fh.close()
                self._fh = None

    @staticmethod
    def probe_busy(path: Path) -> bool:
        if not path.exists():
            return False
        try:
            with open(path, "a+") as fh:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    return True
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                return False
        except OSError:
            return False


def _lock_path(establishment_id, target_date: date) -> Path:
    return LOCKS_DIR / f"est{establishment_id}_{target_date}.lock"


def _global_lock_path() -> Path:
    # Computed lazily (not module-level) so tests can monkeypatch LOCKS_DIR
    # and still have the global lock land in the patched directory.
    return LOCKS_DIR / "global_r365_browser.lock"


class ReconcileLock:
    """Non-blocking advisory file lock for one (establishment, date) pair.

    Usage:
        with ReconcileLock(est_id, target_date, source="cli"):
            open_r365_journal_entry(...)
    """

    def __init__(self, establishment_id, target_date: date, source: str = "unknown"):
        self.establishment_id = establishment_id
        self.target_date = target_date
        self._impl = _FlockFile(
            _lock_path(establishment_id, target_date), source,
            lambda holder: LockBusyError(establishment_id, target_date, holder),
        )

    def __enter__(self) -> "ReconcileLock":
        self._impl.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._impl.release()
        return False


class GlobalBrowserLock:
    """Non-blocking advisory lock covering ALL R365 browser automation
    process-wide — one holder at a time, regardless of which location, date,
    or route (JE posting vs. report-viewer export) is using it. Protects the
    single shared Playwright persistent profile directory.

    Usage:
        with GlobalBrowserLock(source="cli"):
            open_r365_journal_entry(...)   # or open_report_viewer(...)
    """

    def __init__(self, source: str = "unknown"):
        self._impl = _FlockFile(_global_lock_path(), source, BrowserLockBusyError)

    def __enter__(self) -> "GlobalBrowserLock":
        self._impl.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self._impl.release()
        return False


class R365BrowserLock:
    """Composed lock for a single (establishment, date) R365 posting session:
    acquires GlobalBrowserLock first (serializes ALL browser automation),
    then ReconcileLock for the specific location/date (gives a precise error
    if it's that exact pair, rather than just "browser busy"). Released in
    reverse order. This is what call sites should actually use.

    Usage:
        with R365BrowserLock(est_id, target_date, source="cli"):
            open_r365_journal_entry(...)
    """

    def __init__(self, establishment_id, target_date: date, source: str = "unknown"):
        self._global = GlobalBrowserLock(source=source)
        self._local = ReconcileLock(establishment_id, target_date, source=source)

    def __enter__(self) -> "R365BrowserLock":
        self._global.__enter__()
        try:
            self._local.__enter__()
        except Exception:
            self._global.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._local.__exit__(exc_type, exc, tb)
        finally:
            self._global.__exit__(exc_type, exc, tb)
        return False


# A dedicated lock for the report-viewer export path: it doesn't have a
# single (establishment_id, date) — it takes a list of legal entities and an
# optional date range — so it only needs the global browser lock, not the
# per-location ReconcileLock.
def report_viewer_lock(source: str = "unknown") -> GlobalBrowserLock:
    return GlobalBrowserLock(source=source)


def is_locked(establishment_id, target_date: date) -> bool:
    """Best-effort, non-blocking check: True if another process holds the
    per-(location, date) lock."""
    return _FlockFile.probe_busy(_lock_path(establishment_id, target_date))


def is_browser_locked() -> bool:
    """Best-effort, non-blocking check: True if another process holds the
    global R365 browser lock."""
    return _FlockFile.probe_busy(_global_lock_path())
