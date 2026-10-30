"""configure-accounts: credential prompts, atomic .env writes, redaction."""

import argparse
import os
import stat

import pytest

import env_manager
import reconcile_cli as cli


SAMPLE_ENV = """\
REVEL_USER=old_revel@example.com
REVEL_PASS=old_revel_pass

# unrelated comment — must survive
UNRELATED_KEY=unrelated_value

R65_USER=old_r365@example.com
R65_PASS=old_r365_pass
R365_URL=https://ayg.restaurant365.com

R365_API_TOKEN=do-not-touch-token
"""


@pytest.fixture
def env_file(tmp_path):
    p = tmp_path / ".env"
    p.write_text(SAMPLE_ENV, encoding="utf-8")
    return p


# ─── env_manager primitives ──────────────────────────────────────────────────

def test_get_value_reads_existing_key(env_file):
    lines = env_manager.read_lines(env_file)
    assert env_manager.get_value(lines, "REVEL_USER") == "old_revel@example.com"
    assert env_manager.get_value(lines, "NOPE") is None


def test_set_values_preserves_unrelated_lines_and_comments(env_file):
    env_manager.set_values(env_file, {"REVEL_USER": "new_revel@example.com"})
    content = env_file.read_text(encoding="utf-8")
    assert "UNRELATED_KEY=unrelated_value" in content
    assert "# unrelated comment — must survive" in content
    assert "R365_API_TOKEN=do-not-touch-token" in content
    # old value is gone, new one took its place
    assert "old_revel@example.com" not in content
    assert "new_revel@example.com" in content


def test_set_values_handles_special_characters(env_file):
    tricky = 'p@ss"w\'ord with spaces & $igns \\backslash'
    env_manager.set_values(env_file, {"REVEL_PASS": tricky})
    lines = env_manager.read_lines(env_file)
    assert env_manager.get_value(lines, "REVEL_PASS") == tricky


def test_set_values_appends_unknown_key(env_file):
    appended = env_manager.set_values(env_file, {"BRAND_NEW_KEY": "value"})
    assert appended == ["BRAND_NEW_KEY"]
    lines = env_manager.read_lines(env_file)
    assert env_manager.get_value(lines, "BRAND_NEW_KEY") == "value"


def test_set_values_is_atomic_no_tmp_file_left_behind(env_file):
    env_manager.set_values(env_file, {"REVEL_USER": "x@example.com"})
    leftover = list(env_file.parent.glob(".env.*.tmp"))
    assert leftover == []


def test_backup_file_creates_600_permissioned_copy(env_file, tmp_path):
    backup_dir = tmp_path / "env_backups"
    backup_path = env_manager.backup_file(env_file, backup_dir)
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == SAMPLE_ENV
    mode = stat.S_IMODE(os.stat(backup_path).st_mode)
    assert mode == 0o600


# ─── cmd_configure_accounts (interactive flow, mocked input) ────────────────

def _args(env_file):
    return argparse.Namespace(env_file=str(env_file))


def test_configure_accounts_updates_only_requested_account(env_file, monkeypatch):
    answers = iter([
        "y",                       # update revel?
        "n",                       # update r365?
        "new_revel@example.com",   # revel email
        "y",                       # confirm write
    ])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-revel-secret")

    rc = cli.cmd_configure_accounts(_args(env_file))
    assert rc == 0

    lines = env_manager.read_lines(env_file)
    assert env_manager.get_value(lines, "REVEL_USER") == "new_revel@example.com"
    assert env_manager.get_value(lines, "REVEL_PASS") == "new-revel-secret"
    # R365 untouched because we declined it
    assert env_manager.get_value(lines, "R65_USER") == "old_r365@example.com"
    assert env_manager.get_value(lines, "R65_PASS") == "old_r365_pass"
    # API token never touched by this command
    assert env_manager.get_value(lines, "R365_API_TOKEN") == "do-not-touch-token"


def test_configure_accounts_blank_input_retains_existing_value(env_file, monkeypatch):
    answers = iter([
        "y",   # update revel?
        "n",   # update r365?
        "",    # blank email -> keep existing
        "y",   # confirm write
    ])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "")  # blank password -> keep existing

    cli.cmd_configure_accounts(_args(env_file))

    lines = env_manager.read_lines(env_file)
    assert env_manager.get_value(lines, "REVEL_USER") == "old_revel@example.com"
    assert env_manager.get_value(lines, "REVEL_PASS") == "old_revel_pass"


def test_configure_accounts_declining_both_leaves_file_untouched(env_file, monkeypatch):
    original = env_file.read_text(encoding="utf-8")
    answers = iter(["n", "n"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))

    rc = cli.cmd_configure_accounts(_args(env_file))
    assert rc == 0
    assert env_file.read_text(encoding="utf-8") == original


def test_configure_accounts_requires_final_confirmation(env_file, monkeypatch):
    original = env_file.read_text(encoding="utf-8")
    answers = iter([
        "y", "n",
        "new_revel@example.com",
        "n",  # decline the final "write now?" confirmation
    ])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-secret")

    rc = cli.cmd_configure_accounts(_args(env_file))
    assert rc == 1
    assert env_file.read_text(encoding="utf-8") == original  # unchanged


def test_configure_accounts_creates_timestamped_backup_before_writing(env_file, monkeypatch):
    answers = iter(["y", "n", "new_revel@example.com", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-secret")

    cli.cmd_configure_accounts(_args(env_file))

    backups = list((env_file.parent / cli.ENV_BACKUP_DIR).glob(".env.*.bak"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_ENV  # pre-change content
    mode = stat.S_IMODE(os.stat(backups[0]).st_mode)
    assert mode == 0o600


def test_configure_accounts_sets_env_file_permissions_600(env_file, monkeypatch):
    os.chmod(env_file, 0o644)
    answers = iter(["y", "n", "", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: "new-secret")

    cli.cmd_configure_accounts(_args(env_file))

    mode = stat.S_IMODE(os.stat(env_file).st_mode)
    assert mode == 0o600


# ─── Secret redaction ────────────────────────────────────────────────────────

def test_secret_never_printed_to_stdout(env_file, monkeypatch, capsys):
    secret_value = "sUpEr-SeCrEt-Pa$$w0rd-12345"
    answers = iter(["y", "n", "new_revel@example.com", "y"])
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: secret_value)

    cli.cmd_configure_accounts(_args(env_file))

    captured = capsys.readouterr()
    assert secret_value not in captured.out
    assert secret_value not in captured.err
    # And the file on disk should contain it (that's the point), proving the
    # assertion above is a true redaction check, not a tautology.
    assert secret_value in env_file.read_text(encoding="utf-8")


def test_redact_helper_never_returns_input():
    assert cli.redact("literally-anything") == "********"
