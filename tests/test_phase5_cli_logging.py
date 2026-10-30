"""
Phase 5: concise terminal output + detailed, redacted file logging under
logs/cli/, with zero change to accounting/browser/attachment/approval logic.
"""

import argparse
import logging
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


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """_configure_cli_logging() mutates the root logger's handlers process-
    wide; restore them after each test so other tests/sessions aren't
    affected by a stray FileHandler left open."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
    for h in original_handlers:
        root.addHandler(h)
    root.setLevel(original_level)


# ─── _configure_cli_logging ──────────────────────────────────────────────────

def test_configure_cli_logging_creates_timestamped_file_under_logs_cli(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")

    log_path = cli._configure_cli_logging()

    assert log_path.parent == tmp_path / "logs" / "cli"
    assert log_path.name.startswith("reconcile_")
    assert log_path.name.endswith(".log")
    assert log_path.parent.is_dir()


def test_configure_cli_logging_removes_prior_console_handlers(monkeypatch, tmp_path):
    """import server's logging.basicConfig() attaches a console StreamHandler
    -- this must be removed so library INFO logs stop flooding the terminal."""
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    root = logging.getLogger()
    stray = logging.StreamHandler()
    root.addHandler(stray)

    cli._configure_cli_logging()

    assert stray not in root.handlers
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.FileHandler)


def test_configure_cli_logging_writes_library_log_lines_to_file_not_stdout(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    some_library_logger = logging.getLogger("revel.operations")
    some_library_logger.info("Fetching est=%d — OK (%d bytes)", 54, 12345)

    out = capsys.readouterr().out
    assert "Fetching est=54" not in out  # never hits the terminal

    content = log_path.read_text(encoding="utf-8")
    assert "Fetching est=54" in content
    assert "revel.operations" in content


def test_configure_cli_logging_redacts_secrets_in_file(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    some_library_logger = logging.getLogger("revel.operations")
    some_library_logger.error(
        "Unhandled error: cookie: sessionid=realsecretvalue123; csrftoken=othersecret456"
    )

    content = log_path.read_text(encoding="utf-8")
    assert "realsecretvalue123" not in content
    assert "othersecret456" not in content
    assert "[REDACTED]" in content


def test_configure_cli_logging_redacts_format_args_too(monkeypatch, tmp_path):
    """Logging calls that pass secrets as %-style args, not pre-formatted
    into the message string, must also be redacted."""
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    some_library_logger = logging.getLogger("r365.session")
    some_library_logger.info("Request failed: %s", "authorization: Bearer realtoken789")

    content = log_path.read_text(encoding="utf-8")
    assert "realtoken789" not in content
    assert "[REDACTED]" in content


def test_main_prints_log_path_for_run_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("not_found", detail="no JE"))

    rc = cli.main(["run", "--date", "2026-10-25", "--location", "LCF Katy"])

    out = capsys.readouterr().out
    assert "(detailed logs:" in out
    assert str(tmp_path) in out


def test_main_does_not_print_log_path_for_status_command(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")

    cli.main(["status", "--date", "2099-01-01"])

    out = capsys.readouterr().out
    assert "(detailed logs:" not in out


# ─── Concise terminal summary (the actual complaint) ────────────────────────

def test_live_run_terminal_output_has_no_narrative_filler_lines(monkeypatch, tmp_path, capsys):
    """The exact lines Phase 5 complains about must be gone from stdout."""
    fake_je = {"number": "NJ00026791", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": "/tmp/x.pdf"}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "approved", "approved_ok": True, "attachment_status": "uploaded",
    })

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=False)
    rc = cli.cmd_run(args)

    out = capsys.readouterr().out
    assert rc == 0
    assert "Fetching Revel report..." not in out
    assert "Revel fetch OK." not in out
    assert "Posting to R365 (headless browser)" not in out


def test_live_run_terminal_summary_includes_all_required_fields(monkeypatch, tmp_path, capsys):
    """location, date, Revel fetched, JE updated, balanced/unbalanced,
    attachment status, approval status, final success/failure -- all on one
    reportable line."""
    fake_je = {"number": "NJ00026791", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": "/tmp/x.pdf"}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": True, "je_difference": 0.0,
        "approved": "approved", "approved_ok": True, "attachment_status": "uploaded",
    })

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=False)
    cli.cmd_run(args)

    out = capsys.readouterr().out
    assert "LCF Cypress" in out          # location
    assert "2026-10-22" in out           # date
    assert "Revel: OK" in out            # Revel fetched
    assert "NJ00026791: updated" in out  # JE updated
    assert "balanced: yes" in out        # balanced/unbalanced
    assert "attachment: uploaded" in out # attachment status
    assert "approved: approved" in out   # approval status
    assert "SUCCESS" in out              # final success/failure


def test_live_run_terminal_summary_shows_failure_and_cause(monkeypatch, tmp_path, capsys):
    fake_je = {"number": "NJ1", "status": "Unapproved", "debitTotal": 1, "creditTotal": 1}
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("ready", je=fake_je, match_count=1))
    monkeypatch.setattr(cli, "fetch_reports", lambda *a, **k: [
        {"establishment_id": 54, "data": FAKE_REVEL_DATA, "pdf_path": None}
    ])
    import reconcile_lock
    monkeypatch.setattr(reconcile_lock, "LOCKS_DIR", tmp_path)
    monkeypatch.setattr(cli, "open_r365_journal_entry", lambda *a, **k: {
        "status": "ok", "je_balanced": False, "je_difference": 15.0,
        "approved": "skipped", "approved_ok": False, "attachment_status": "skipped",
    })

    args = argparse.Namespace(date="2026-10-22", all=False, location=["LCF Cypress"],
                               dry_run=False, update_only=False)
    rc = cli.cmd_run(args)

    out = capsys.readouterr().out
    assert rc == 1
    assert "balanced: NO" in out
    assert "FAILED" in out


# ─── Existing functionality / substrings preserved (regression) ────────────

def test_blocked_prefix_and_comparison_substrings_still_present(monkeypatch, capsys):
    """Guards against the logging cleanup silently changing wording that
    other tests (and operators) depend on."""
    monkeypatch.setattr(cli, "locate_existing_je",
                         lambda *a, **k: cli.JELocateResult("already_approved", detail="JE-1 is Approved"))
    args = argparse.Namespace(date="2026-10-25", all=False, location=["LCF Katy"], dry_run=False)
    cli.cmd_run(args)
    assert "BLOCKED" in capsys.readouterr().out


def test_extract_revel_values_unchanged_by_logging_work():
    """Static guard: Phase 5 must not have touched accounting logic."""
    values = server._extract_revel_values(FAKE_REVEL_DATA)
    assert values["food_sales"] == 700.0
    assert values["beverage_sales"] == 300.0
    assert values["sales_tax"] == 80.0


# ─── Regression: dict %s argument crashed _RedactingLogFilter ──────────────
#
# log.info("...: %s", {...}) stores the dict itself as record.args (not a
# 1-tuple containing it) -- logging's own special case for dict-style %-args.
# The filter used to do `tuple(... for a in record.args)` unconditionally,
# which iterates a dict's *keys*, turning {"adj_total": 1.23} into
# ("adj_total",) and breaking that record's "%s" formatting later
# (TypeError: not all arguments converted during string formatting). This
# is exactly server.py's "sales_data adj/tip/fee fields: %s" call.

def test_dict_single_arg_reaches_file_without_logging_traceback(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    some_library_logger = logging.getLogger("server")
    some_library_logger.info(
        "sales_data adj/tip/fee fields: %s",
        {"adj_total": 1.23, "adj_credit_tips": 5.0, "tip_in": 0.0},
    )

    content = log_path.read_text(encoding="utf-8")
    assert "Logging error" not in content
    assert "not all arguments converted" not in content
    assert "adj_total" in content
    assert "1.23" in content


def test_dict_single_arg_does_not_print_logging_traceback_to_stderr(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    cli._configure_cli_logging()

    logging.getLogger("server").info(
        "sales_data adj/tip/fee fields: %s", {"adj_total": 1.23}
    )

    err = capsys.readouterr().err
    assert "TypeError" not in err
    assert "not all arguments converted" not in err


def test_dict_single_arg_with_secret_value_is_redacted(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    logging.getLogger("r365.session").info(
        "headers: %s", {"Authorization": "authorization: Bearer realtoken789", "ok": True}
    )

    content = log_path.read_text(encoding="utf-8")
    assert "realtoken789" not in content
    assert "[REDACTED]" in content


def test_named_dict_placeholders_still_format_correctly(monkeypatch, tmp_path):
    """"%(key)s"-style formatting also passes a single dict as record.args --
    must keep working (and keep redacting string values) after the fix."""
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    logging.getLogger("revel.operations").info(
        "user=%(user)s token=%(token)s",
        {"user": "bob", "token": "authorization: Bearer realtoken789"},
    )

    content = log_path.read_text(encoding="utf-8")
    assert "user=bob" in content
    assert "realtoken789" not in content
    assert "[REDACTED]" in content


def test_numeric_and_mixed_positional_args_still_format_correctly(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "CLI_LOGS_DIR", tmp_path / "logs" / "cli")
    log_path = cli._configure_cli_logging()

    logging.getLogger("revel.operations").info("Fetching est=%d — OK (%d bytes)", 6, 27985)
    logging.getLogger("revel.operations").info("fetched %s rows in %.2fs", 42, 1.5)

    content = log_path.read_text(encoding="utf-8")
    assert "Logging error" not in content
    assert "Fetching est=6 — OK (27985 bytes)" in content
    assert "fetched 42 rows in 1.50s" in content
