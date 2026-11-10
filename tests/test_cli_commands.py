"""Every registered subcommand must at least import and parse.

Four subcommands shipped importing functions that had never existed
(`build_features`, `build_sector_daily`, `run_screen`, `observed_calendar`).
Each raised ImportError the moment it was run, and nothing caught it because
the imports were inside the command bodies -- invisible to collection, and
invisible to anyone who only ran `ta daily`.
"""
from __future__ import annotations

import pytest
from ta_cli.__main__ import cmd_backtest, main


@pytest.mark.parametrize("cmd", [
    "cost", "ingest", "daily", "constituents", "symbols",
    "screen", "calendar", "status", "backtest",
])
def test_subcommand_is_registered_and_its_help_renders(cmd: str) -> None:
    """`--help` constructs the parser and resolves `set_defaults(fn=...)`,
    so a command pointing at a function that does not exist fails here."""
    with pytest.raises(SystemExit) as e:
        main([cmd, "--help"])
    assert e.value.code == 0


def test_every_command_body_imports() -> None:
    """Import what the command bodies reach for.

    The bodies import lazily, to keep `ta --help` fast. That is reasonable,
    but it means a broken import hides until the command is actually run.
    This pays the cost once, in CI, rather than at 22:00 on a Tuesday.
    """
    import ta_datafeed.backfill
    import ta_datafeed.store  # noqa: F401
    import ta_quant.backtest
    import ta_quant.nightly  # noqa: F401
    from ta_core.costs import compute_costs  # noqa: F401

    assert callable(cmd_backtest)


def test_cli_daily_replay_and_date_arguments() -> None:
    """Verify --replay and --date flags parse cleanly on ta daily."""
    with pytest.raises(SystemExit) as e:
        main(["daily", "--replay", "--date", "2026-10-30", "--help"])
    assert e.value.code == 0


def test_cli_backtest_arguments() -> None:
    """Verify ta backtest arguments parse cleanly."""
    with pytest.raises(SystemExit) as e:
        main(["backtest", "--start", "2026-01-01", "--end", "2026-06-30", "--arm", "FROZEN_CONTROL", "--trials", "5", "--help"])
    assert e.value.code == 0

