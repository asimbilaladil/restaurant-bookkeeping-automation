"""
r365_profile_tools.py
======================
Backs up and safely invalidates the persistent R365 Playwright browser
profile (~/.r365_browser_profile, see r365/session.py:PROFILE_DIR) when the
R365 browser-login credentials (R65_USER/R65_PASS) are rotated.

Why this matters: the profile directory holds Chromium's cached cookies and
local storage for the R365 session. After rotating credentials, leaving the
old profile in place risks `ensure_logged_in_r365()` finding what still looks
like a valid cached session for the OLD account and silently continuing to
use it instead of logging in as the new one.

This module never deletes anything:
  1. backup_profile()    - tars the current profile into a timestamped,
                            permission-restricted archive.
  2. invalidate_profile() - RENAMES (not deletes) the live profile directory
                            aside to a sibling ".invalidated_<timestamp>"
                            directory. The next browser launch recreates a
                            fresh, empty profile at the original path and is
                            forced to log in again — with whatever
                            credentials are current in .env at that time.
                            The old, renamed directory is left on disk
                            (recoverable) until an operator deletes it.
"""

from __future__ import annotations

import os
import stat
import tarfile
from datetime import datetime, timezone
from pathlib import Path

# Chromium's own render/compile caches inside the profile — purely
# reproducible (regenerated automatically on first use), hold zero session
# or login state, and were observed to be ~620MB of a ~636MB profile on this
# host. Excluded so a routine credential-rotation backup doesn't balloon;
# every byte of actual session state (Cookies, Local Storage, IndexedDB,
# Login Data, History, Sessions, ...) is still backed up in full.
_REPRODUCIBLE_CACHE_DIR_NAMES = {
    "Cache", "Code Cache", "GPUCache", "DawnWebGPUCache", "DawnGraphiteCache",
    "ShaderCache", "GrShaderCache", "GraphiteDawnCache",
}


def _skip_reproducible_cache(tarinfo: tarfile.TarInfo) -> tarfile.TarInfo | None:
    parts = Path(tarinfo.name).parts
    if any(p in _REPRODUCIBLE_CACHE_DIR_NAMES for p in parts):
        return None  # tarfile.add(filter=...) returning None skips this member
    return tarinfo


def backup_profile(profile_dir: str | Path, backup_dir: str | Path) -> Path | None:
    """Tar `profile_dir` into `backup_dir` as a timestamped, 600-permission
    archive. Returns the archive path, or None if there is no profile to
    back up yet (first-ever login hasn't happened).

    Skips Chromium's reproducible render/compile caches (see
    _REPRODUCIBLE_CACHE_DIR_NAMES) — not an omission, a deliberate size
    reduction; no session-relevant data lives in those directories."""
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return None

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(backup_dir, stat.S_IRWXU)  # 700 — owner only

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = backup_dir / f"r365_browser_profile_{ts}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tar:
        tar.add(profile_dir, arcname=profile_dir.name, filter=_skip_reproducible_cache)
    os.chmod(archive_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    return archive_path


def invalidate_profile(profile_dir: str | Path) -> Path | None:
    """Rename the live profile directory aside so the next browser launch
    starts fresh and is forced to re-authenticate. Returns the path it was
    renamed to, or None if there was no profile directory to invalidate."""
    profile_dir = Path(profile_dir)
    if not profile_dir.exists():
        return None

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    invalidated_path = profile_dir.parent / f"{profile_dir.name}.invalidated_{ts}"
    os.rename(profile_dir, invalidated_path)
    return invalidated_path


def rotate_profile(profile_dir: str | Path, backup_dir: str | Path) -> dict:
    """Back up, then invalidate, the R365 browser profile. Call this only
    when R65_USER/R65_PASS actually changed. Returns a dict describing what
    happened (paths only — never touches or logs credential values)."""
    backup_path = backup_profile(profile_dir, backup_dir)
    invalidated_path = invalidate_profile(profile_dir) if backup_path is not None else None
    return {
        "had_existing_profile": backup_path is not None,
        "backup_path": str(backup_path) if backup_path else None,
        "invalidated_path": str(invalidated_path) if invalidated_path else None,
    }
