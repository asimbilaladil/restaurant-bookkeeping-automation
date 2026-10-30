"""
Test isolation setup.

Critical: this file runs BEFORE any test module imports `reconcile_cli` (and
therefore `server`, which calls db.init_db() and load_dotenv() at import
time). We must guarantee that import chain never touches the real production
`.env` secrets or the real `dss_runs.db` — so:

  1. Pre-set every secret-shaped env var the app reads at import time to a
     harmless dummy value. python-dotenv's load_dotenv() defaults to
     override=False, so once these are set, the real .env file's values for
     the same keys are never read into the process, even though
     `server.py` still calls load_dotenv() itself.
  2. Point db.DB_PATH at a throwaway temp file BEFORE `server` (which calls
     db.init_db() at import time) is ever imported, so no test can write to
     the production dss_runs.db.
"""

import os
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

_DUMMY_ENV = {
    "FLASK_SECRET_KEY": "test-secret",
    "LOGIN_USERNAME": "test-user",
    "LOGIN_PASSWORD_HASH": "pbkdf2:sha256:1$test$0000000000000000000000000000000000000000000000000000000000000000",
    "DSS_RUNS_API_TOKEN": "",
    "NET_CASH_REFUNDS": "true",
    "REVEL_USER": "dummy_revel_user@example.test",
    "REVEL_PASS": "dummy-revel-pass",
    "R65_USER": "dummy_r365_user@example.test",
    "R65_PASS": "dummy-r365-pass",
    "R365_URL": "https://example.test",
    "R365_API_BASE_URL": "",
    "R365_API_TOKEN": "",
    "R365_API_TENANT_ID": "",
    "R365_API_SECURITY_ID": "",
}
for k, v in _DUMMY_ENV.items():
    os.environ.setdefault(k, v)

# Redirect the SQLite DB to a throwaway temp file before `server` (imported by
# reconcile_cli) calls db.init_db() at import time.
import db  # noqa: E402

_TEST_DB_FD, _TEST_DB_PATH = tempfile.mkstemp(suffix="_test_dss_runs.db")
os.close(_TEST_DB_FD)
db.DB_PATH = Path(_TEST_DB_PATH)
db.init_db()

assert "opt/accounts/app/dss_runs.db" not in str(db.DB_PATH), (
    "Refusing to run tests against the production database path."
)
