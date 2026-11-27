"""The `bt2live run` command line. These tests need no extra and run on every supported Python."""

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

from backtest2live import cli, run

HAS_EXTRA = sys.version_info >= (3, 12) and importlib.util.find_spec("trading_engine_interface") is not None


def parse(*argv):
    return cli.build_parser().parse_args(["run", *argv])


def entry(name, module):
    return types.SimpleNamespace(name=name, module=module)


class Registry:
    def __init__(self, *entries):
        self._entries = list(entries)

    def list_entries(self):
        return self._entries


def test_fee_defaults_are_retail_not_zero():
    args = parse("my_pkg", "--pair", "BTC-USDT", "--days", "3")
    assert (args.taker_fee_bps, args.maker_fee_bps, args.slippage_bps, args.spread_bps) == (10, 8, 5, 10)
    assert args.venue == "okx" and args.capital == "100000"


def test_window_from_days_ends_now_and_from_dates_is_utc():
    start, end = run._window(parse("my_pkg", "--pair", "BTC-USDT", "--days", "3"))
    assert end - start == timedelta(days=3) and datetime.now(timezone.utc) - end < timedelta(minutes=2)
    window = run._window(parse("my_pkg", "--pair", "BTC-USDT", "--from", "2026-01-01", "--to", "2026-01-02 06:30"))
    assert window == (datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 2, 6, 30, tzinfo=timezone.utc))


def test_set_values_are_parsed_as_json_with_bare_words_as_strings():
    assert run._overrides(["quantity=2", "flag=true", "name=fast", "levels=[1, 2]"]) == {
        "quantity": 2, "flag": True, "name": "fast", "levels": [1, 2],
    }


def test_strategy_is_picked_from_the_named_package_only():
    registry = Registry(entry("Mine", "my_pkg.strat"), entry("Other", "someone_else"), entry("Near", "my_pkg_two"))
    assert run._pick_strategy(registry, "my_pkg", None) == "Mine"
    assert run._pick_strategy(Registry(entry("Solo", "single_mod")), "single_mod", None) == "Solo"


@pytest.mark.parametrize("entries,wanted,message", [
    ([], None, "registers no strategy"),
    ([entry("A", "my_pkg.a"), entry("B", "my_pkg.b")], None, "Pick one with --strategy"),
    ([entry("A", "my_pkg.a")], "Z", "registers no strategy named 'Z'"),
])
def test_strategy_pick_errors_say_what_to_do(entries, wanted, message):
    with pytest.raises(SystemExit) as e:
        run._pick_strategy(Registry(*entries), "my_pkg", wanted)
    assert message in str(e.value)
    assert run._pick_strategy(Registry(entry("A", "my_pkg.a"), entry("B", "my_pkg.b")), "my_pkg", "B") == "B"


@pytest.mark.parametrize("argv,message", [
    (["--from", "2026-01-01"], "--from needs --to"),
    (["--from", "2026-01-02", "--to", "2026-01-01"], "--to must be after --from"),
    (["--from", "yesterday", "--to", "2026-01-01"], "--from 'yesterday' is not a date"),
    (["--days", "0"], "--days must be at least 1"),
    (["--days", "1", "--set", "quantity"], "--set 'quantity' is not KEY=VALUE"),
    (["--days", "1", "--capital", "lots"], "--capital 'lots' is not a number"),
    (["--days", "1", "--capital", "0"], "--capital must be positive"),
])
def test_bad_arguments_are_an_error_not_a_verdict(capsys, argv, message):
    assert cli.main(["run", "my_pkg", "--pair", "BTC-USDT", *argv]) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err


def test_a_window_is_required(capsys):
    with pytest.raises(SystemExit) as e:
        parse("my_pkg", "--pair", "BTC-USDT")
    assert e.value.code == cli.EXIT_ERROR


def test_unknown_package_is_named_in_the_error(tmp_path, capsys):
    rc = cli.main(["run", "no_such_pkg_xyz", "--path", str(tmp_path), "--pair", "BTC-USDT", "--days", "1"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_ERROR
    if sys.version_info >= (3, 12):
        assert "no package or module named 'no_such_pkg_xyz'" in err
    else:
        assert "needs Python 3.12+" in err


@pytest.mark.skipif(HAS_EXTRA, reason="the run extra is installed")
def test_without_the_extra_the_command_says_how_to_install_it(tmp_path, capsys):
    (tmp_path / "plain_mod_for_run.py").write_text("x = 1\n", encoding="utf-8")
    rc = cli.main(["run", "plain_mod_for_run", "--path", str(tmp_path), "--pair", "BTC-USDT", "--days", "1"])
    assert rc == cli.EXIT_ERROR
    assert "pip install 'backtest2live[run]'" in capsys.readouterr().err


@pytest.mark.skipif(not HAS_EXTRA, reason="needs Python 3.12+ and the run extra")
def test_with_the_extra_a_strategy_outside_any_repo_is_found(tmp_path):
    (tmp_path / "bt2live_run_fixture.py").write_text(
        "from trading_engine_interface import BaseStrategyConfig\n"
        "from trading_engine_interface.runtime import register_strategy\n\n\n"
        "class Settings(BaseStrategyConfig):\n    quantity: int = 1\n\n\n"
        "@register_strategy(config=Settings)\nclass FixtureStrategy:\n    VERSION: int = 1\n",
        encoding="utf-8",
    )
    replay, registry = run._load("bt2live_run_fixture", str(tmp_path))
    try:
        assert run._pick_strategy(registry, "bt2live_run_fixture", None) == "FixtureStrategy"
        from trading_engine_interface import Symbol

        # Settings declares neither class_type nor position, so the runner supplies both.
        config = replay.build_config(registry, "FixtureStrategy", [Symbol.BTC_USDT], 1000, 5.0, {})
        assert type(config).__name__ == "Settings" and config.class_type == "FixtureStrategy"
    finally:
        registry.unregister("FixtureStrategy")
