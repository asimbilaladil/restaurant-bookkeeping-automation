"""CLI argument validation, date parsing, location selection."""

import argparse
from datetime import date, datetime, timedelta, timezone

import pytest

import reconcile_cli as cli


# ─── Date parsing ───────────────────────────────────────────────────────────

def test_parse_date_explicit_iso():
    assert cli.parse_date_arg("2026-10-25") == date(2026, 10, 25)


def test_parse_date_today():
    assert cli.parse_date_arg("today") == datetime.now(timezone.utc).date()


def test_parse_date_yesterday():
    expected = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert cli.parse_date_arg("yesterday") == expected


def test_parse_date_case_insensitive():
    assert cli.parse_date_arg("Yesterday") == cli.parse_date_arg("yesterday")


@pytest.mark.parametrize("bad", ["not-a-date", "2026/10/25", "", "13-99-99"])
def test_parse_date_invalid_raises(bad):
    with pytest.raises(argparse.ArgumentTypeError):
        cli.parse_date_arg(bad)


# ─── Location resolution ─────────────────────────────────────────────────────

def test_resolve_locations_all():
    ids = cli.resolve_locations(True, None)
    assert ids == list(cli.DEFAULT_ESTABLISHMENTS)


def test_resolve_locations_single_exact_name():
    ids = cli.resolve_locations(False, ["LCF Beaumont"])
    assert ids == [14]


def test_resolve_locations_case_insensitive():
    ids = cli.resolve_locations(False, ["lcf beaumont"])
    assert ids == [14]


def test_resolve_locations_multiple_preserve_order():
    ids = cli.resolve_locations(False, ["LCF Katy", "LCF Airtex"])
    assert ids == [6, 32]


def test_resolve_locations_unknown_name_raises():
    with pytest.raises(ValueError, match="unknown location"):
        cli.resolve_locations(False, ["Not A Real Location"])


def test_resolve_locations_both_all_and_location_raises():
    with pytest.raises(ValueError, match="not both"):
        cli.resolve_locations(True, ["LCF Katy"])


def test_resolve_locations_neither_raises():
    with pytest.raises(ValueError, match="--all or"):
        cli.resolve_locations(False, None)


# ─── argparse wiring (full parser) ───────────────────────────────────────────

def test_run_requires_date():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--all"])


def test_run_all_and_location_are_mutually_exclusive_at_parser_level():
    parser = cli.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["run", "--date", "2026-10-25", "--all", "--location", "LCF Katy"])


def test_run_parses_dry_run_flag():
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--date", "2026-10-25", "--all", "--dry-run"])
    assert args.dry_run is True
    assert args.all is True


def test_status_defaults():
    parser = cli.build_parser()
    args = parser.parse_args(["status"])
    assert args.date is None
    assert args.location is None
    assert args.limit == 50


def test_configure_accounts_default_env_file():
    parser = cli.build_parser()
    args = parser.parse_args(["configure-accounts"])
    assert args.env_file == ".env"


def test_future_date_rejected_by_cmd_run(capsys):
    parser = cli.build_parser()
    future = (datetime.now(timezone.utc).date() + timedelta(days=5)).isoformat()
    args = parser.parse_args(["run", "--date", future, "--all"])
    rc = cli.cmd_run(args)
    assert rc == 2
    assert "future" in capsys.readouterr().err
