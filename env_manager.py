"""
env_manager.py
===============
Safe, atomic read/update helpers for a dotenv-style file (.env).

Design goals (used by `reconcile_cli.py configure-accounts`):
  - Preserve every line we don't touch verbatim — comments, blank lines,
    ordering, unrelated keys.
  - Never partially write the file: build the full new content in memory,
    write it to a temp file in the SAME directory, then os.replace() it over
    the original. os.replace is atomic on the same filesystem, so a crash or
    power loss mid-write can never leave a half-written .env.
  - Take a permission-restricted, timestamped backup of the original file
    before every change.
  - Quote values that need it (anything with whitespace, '#', quotes, '$',
    or backslashes) using dotenv-compatible double-quote escaping, so
    special characters in passwords round-trip safely through python-dotenv.
  - Never log or return the values themselves in any exception message.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# Characters that force double-quoting so python-dotenv (and a human skimming
# the file) parses the value as one token instead of breaking on it.
_NEEDS_QUOTING_RE = re.compile(r'[\s#"\'\\$]')


def _quote_value(value: str) -> str:
    """Double-quote + escape a value if it contains anything dotenv-unsafe.

    Always quoting credential values (regardless of content) is simpler and
    safer than trying to special-case "looks plain" strings — a password
    that happens to look plain today could gain a space or '#' on a future
    rotation and silently truncate on read if we'd left it bare.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_lines(path: str | Path) -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    return p.read_text(encoding="utf-8").splitlines(keepends=False)


def get_value(lines: list[str], key: str) -> str | None:
    """Return the raw (unquoted) value currently set for `key`, or None."""
    for line in lines:
        m = _KEY_RE.match(line)
        if m and m.group(1) == key:
            raw = m.group(2).strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
                inner = raw[1:-1]
                if raw[0] == '"':
                    inner = inner.replace('\\"', '"').replace("\\\\", "\\")
                return inner
            return raw
    return None


def backup_file(path: str | Path, backup_dir: str | Path) -> Path:
    """Copy `path` into `backup_dir` as a timestamped, 600-permission file.

    Returns the backup path. No-op (raises) only if the source is missing —
    callers should create an empty .env first if one genuinely doesn't exist
    yet, so "no prior credentials" is still a deliberate, visible state.
    """
    src = Path(path)
    bdir = Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    os.chmod(bdir, stat.S_IRWXU)  # 700 — owner only

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = bdir / f".env.{ts}.bak"
    content = src.read_text(encoding="utf-8") if src.exists() else ""
    dest.write_text(content, encoding="utf-8")
    os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)  # 600
    return dest


def set_values(path: str | Path, updates: dict[str, str]) -> list[str]:
    """Atomically set `updates` {KEY: value} in the dotenv file at `path`.

    Existing KEY=... lines are replaced in place (value only — any trailing
    inline comment on that line is dropped, matching how the rest of this
    file's keys are written). Keys not already present are appended at the
    end, after a blank line separator if the file is non-empty.

    Returns the list of keys that were newly appended (as opposed to updated
    in place) so the caller can log that distinction without ever logging
    the values.
    """
    p = Path(path)
    lines = read_lines(p)
    remaining = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        m = _KEY_RE.match(line)
        if m and m.group(1) in remaining:
            key = m.group(1)
            new_lines.append(f"{key}={_quote_value(remaining.pop(key))}")
        else:
            new_lines.append(line)

    appended = list(remaining.keys())
    if remaining:
        if new_lines and new_lines[-1].strip() != "":
            new_lines.append("")
        for key, value in remaining.items():
            new_lines.append(f"{key}={_quote_value(value)}")

    new_content = "\n".join(new_lines) + "\n"

    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(p.parent), prefix=".env.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 600 before it's visible under the real name
        os.replace(tmp_path, p)  # atomic on the same filesystem
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    return appended
