import importlib.util
import sys
import types

import pytest

from backtest2live import cli, run

HAS_RUNNER = sys.version_info >= (3, 12) and importlib.util.find_spec("trading_engine_backtest") is not None


def parse(*argv):
    return cli.build_parser().parse_args(["run", *argv])


def entry(name, module):
    return types.SimpleNamespace(name=name, module=module)


class Registry:
    def __init__(self, *entries):
        self._entries = list(entries)

    def list_entries(self):
        return self._entries


def test_arguments_map_onto_the_runner_command_line():
    args = parse("my_pkg", "--pair", "BTC-USDT,ETH-USDT", "--venue", "binance", "--from", "2026-01-01",
                 "--to", "2026-01-02", "--capital", "5000", "--taker-fee-bps", "4", "--maker-fee-bps", "-1",
                 "--set", "quantity=2", "--set", "flag=true", "--export-orders", "orders.json", "--verbose")
    assert run._runner_argv(args, "MyStrategy")[1:] == [
        "--strategy", "MyStrategy", "--symbols", "BTC-USDT,ETH-USDT", "--kline-venue", "BINANCE",
        "--capital", "5000.0", "--taker-fee", "4", "--maker-fee", "-1", "--slippage", "5", "--spread", "10",
        "--run-interval", "5.0", "--start", "2026-01-01", "--end", "2026-01-02",
        "--set", "quantity=2", "--set", "flag=true", "--export-orders", "orders.json", "--verbose",
    ]


def test_fee_defaults_are_retail_not_zero():
    argv = run._runner_argv(parse("my_pkg", "--pair", "BTC-USDT", "--days", "3"), "S")
    assert argv[argv.index("--taker-fee") + 1] == "10" and argv[argv.index("--maker-fee") + 1] == "8"
    assert argv[argv.index("--days") + 1] == "3" and "--start" not in argv


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
    (["my_pkg", "--pair", "BTC-USDT", "--from", "2026-01-01"], "--from needs --to"),
    (["my_pkg", "--pair", "BTC-USDT", "--days", "0"], "--days must be at least 1"),
])
def test_bad_window_is_an_error_not_a_verdict(capsys, argv, message):
    assert cli.main(["run", *argv]) == cli.EXIT_ERROR
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


@pytest.mark.skipif(HAS_RUNNER, reason="the run extra is installed")
def test_without_the_extra_the_command_says_how_to_install_it(tmp_path, capsys):
    (tmp_path / "plain_mod_for_run.py").write_text("x = 1\n", encoding="utf-8")
    rc = cli.main(["run", "plain_mod_for_run", "--path", str(tmp_path), "--pair", "BTC-USDT", "--days", "1"])
    assert rc == cli.EXIT_ERROR
    assert "pip install 'backtest2live[run]'" in capsys.readouterr().err


def test_a_run_that_does_not_complete_is_an_error(monkeypatch, capsys):
    registry = Registry(entry("Mine", "my_pkg"))
    for report, code in (("Status: COMPLETED\n", 0), ("Status: FAILED\nError: boom\n", cli.EXIT_ERROR)):
        runner = types.SimpleNamespace(main=lambda report=report: print(report, end=""))
        monkeypatch.setattr(run, "_load_runner", lambda package, path: (runner, registry))
        assert cli.main(["run", "my_pkg", "--pair", "BTC-USDT", "--days", "1"]) == code
        captured = capsys.readouterr()
        assert report in captured.out
        assert ("optimistic end" in captured.out) == (code == 0)


def test_a_runner_that_exits_is_an_error_not_a_verdict(monkeypatch, capsys):
    def exits():
        print("Unknown config field(s)")
        sys.exit(1)

    monkeypatch.setattr(run, "_load_runner",
                        lambda package, path: (types.SimpleNamespace(main=exits), Registry(entry("Mine", "my_pkg"))))
    assert cli.main(["run", "my_pkg", "--pair", "BTC-USDT", "--days", "1"]) == cli.EXIT_ERROR
    assert "did not start" in capsys.readouterr().err


@pytest.mark.skipif(not HAS_RUNNER, reason="needs Python 3.12+ and the run extra")
def test_with_the_extra_a_strategy_outside_any_repo_is_found_and_configured(tmp_path):
    (tmp_path / "bt2live_run_fixture.py").write_text(
        "from trading_engine_interface import BaseStrategyConfig\n"
        "from trading_engine_interface.runtime import register_strategy\n\n\n"
        "class Settings(BaseStrategyConfig):\n    quantity: int = 1\n\n\n"
        "@register_strategy(config=Settings)\nclass FixtureStrategy:\n    VERSION: int = 1\n",
        encoding="utf-8",
    )
    runner, registry = run._load_runner("bt2live_run_fixture", str(tmp_path))
    try:
        assert run._pick_strategy(registry, "bt2live_run_fixture", None) == "FixtureStrategy"
        from trading_engine_interface import Symbol

        config = runner._load_strategy_config("FixtureStrategy", [Symbol.BTC_USDT])
        assert type(config).__name__ == "Settings" and config.class_type == "FixtureStrategy"
    finally:
        registry.unregister("FixtureStrategy")
