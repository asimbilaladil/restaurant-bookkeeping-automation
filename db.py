"""
db.py
=====
Lightweight SQLite logging for Daily Sales Reconciliation runs.

Every time the reconciliation runs for an establishment we INSERT a new row —
we never overwrite. Re-running the same establishment for the same date simply
appends another record, so the full history (fail → success) is preserved and
visible on the DSS Runs page.
"""

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "dss_runs.db"

# SQLite connections aren't shareable across threads; the reconcile workers run
# in their own threads, so we serialise writes with a simple lock and open a
# short-lived connection per operation.
_lock = threading.Lock()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create the runs table if it doesn't exist. Safe to call on every startup."""
    with _lock, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dss_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                establishment_id    INTEGER,
                establishment_name  TEXT,
                run_date            TEXT,        -- the date being reconciled (YYYY-MM-DD)
                status              TEXT,         -- 'success' or 'error'
                error               TEXT,
                je_difference       REAL,
                je_balanced         INTEGER,
                attachment_status   TEXT,
                approved            TEXT,         -- 'approved' | 'failed' | 'skipped'
                log_filename        TEXT,
                created_at          TEXT          -- UTC timestamp of when this run executed
            )
            """
        )
        # Migration: add `approved` to tables created before this column existed.
        existing = {row[1] for row in conn.execute("PRAGMA table_info(dss_runs)")}
        if "approved" not in existing:
            conn.execute("ALTER TABLE dss_runs ADD COLUMN approved TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dss_runs_run_date ON dss_runs (run_date)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dss_runs_est ON dss_runs (establishment_id)"
        )


def record_run(
    *,
    establishment_id,
    establishment_name,
    run_date,
    status,
    error=None,
    je_difference=None,
    je_balanced=None,
    attachment_status=None,
    approved=None,
    log_filename=None,
) -> int:
    """Insert a new run record and return its row id."""
    created_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _lock, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO dss_runs (
                establishment_id, establishment_name, run_date, status, error,
                je_difference, je_balanced, attachment_status, approved,
                log_filename, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                establishment_id,
                establishment_name,
                str(run_date) if run_date is not None else None,
                status,
                error,
                je_difference,
                1 if je_balanced else 0 if je_balanced is not None else None,
                attachment_status,
                approved,
                log_filename,
                created_at,
            ),
        )
        return cur.lastrowid


def _filter_sql(run_date, establishment_id, status=None) -> tuple[str, list]:
    """Build a shared WHERE clause + params for get_runs / count_runs."""
    clauses = []
    params: list = []
    if run_date:
        clauses.append("run_date = ?")
        params.append(run_date)
    if establishment_id is not None:
        clauses.append("establishment_id = ?")
        params.append(establishment_id)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return where, params


def get_runs(limit: int = 500, offset: int = 0, run_date: str | None = None,
             establishment_id=None) -> list[dict]:
    """Return a page of run records, newest first, optionally filtered."""
    where, params = _filter_sql(run_date, establishment_id)
    params += [limit, offset]
    with _lock, _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM dss_runs{where} ORDER BY id DESC LIMIT ? OFFSET ?", params
        ).fetchall()
    return [dict(r) for r in rows]


def count_runs(run_date: str | None = None, establishment_id=None,
               status: str | None = None) -> int:
    """Count run records matching the filters (for pagination + summary totals)."""
    where, params = _filter_sql(run_date, establishment_id, status)
    with _lock, _connect() as conn:
        (n,) = conn.execute(f"SELECT COUNT(*) FROM dss_runs{where}", params).fetchone()
    return n
