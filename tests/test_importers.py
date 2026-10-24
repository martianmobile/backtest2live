import csv

import pytest

from backtest2live import cli, importers

FIX = "tests/fixtures/adapters"
T0 = 1_711_756_860_000  # 2024-03-30T00:01:00Z, ms


def _run(root, tmp_path, engine, name, *extra):
    out = tmp_path / "orders.csv"
    rc = cli.main(["import", engine, str(root / FIX / name), "-o", str(out), *extra])
    assert rc == 0
    with open(out, newline="") as fh:
        rows = list(csv.DictReader(fh))
    return out, rows


def _by_id(rows):
    return {r["order_id"]: r for r in rows}


def test_header_is_the_schema(root, tmp_path):
    _, rows = _run(root, tmp_path, "vectorbt", "vectorbt_orders.csv")
    assert tuple(rows[0]) == importers.COLUMNS


def test_nautilus_resting_and_market(root, tmp_path):
    _, rows = _run(root, tmp_path, "nautilus", "nautilus_orders_report.csv")
    o = _by_id(rows)
    buy = o["O-20240330-000000-001-001-1"]
    assert (buy["symbol"], buy["ts"], buy["side"], buy["type"], buy["price"]) == \
        ("BTCUSDT-PERP.BINANCE", str(T0), "buy", "limit", "69924.7")
    assert (buy["filled"], buy["fill_price"], buy["end_ts"]) == ("1", "69924.7", str(T0 + 11_234))
    cancel = o["O-20240330-000000-001-001-2"]
    assert (cancel["filled"], cancel["fill_qty"], cancel["fill_price"], cancel["end_ts"]) == \
        ("0", "0", "", str(T0 + 30_000))
    mkt = o["O-20240330-000000-001-001-3"]
    assert (mkt["type"], mkt["price"], mkt["fill_price"]) == ("market", "", "69925.1")


def test_lean_events_fold_into_orders(root, tmp_path):
    _, rows = _run(root, tmp_path, "lean", "lean-order-events.json")
    o = _by_id(rows)
    assert (o["1"]["type"], o["1"]["filled"], o["1"]["end_ts"]) == ("limit", "1", str(T0 + 11_200))
    assert (o["2"]["side"], o["2"]["size"], o["2"]["filled"], o["2"]["end_ts"]) == \
        ("sell", "0.01", "0", str(T0 + 30_000))
    # two partial fills: size-weighted average
    assert o["3"]["type"] == "market" and float(o["3"]["fill_price"]) == pytest.approx(69925.1)
    assert float(o["3"]["fill_qty"]) == pytest.approx(0.02)


@pytest.mark.parametrize("name", ["backtrader_transactions.json", "backtrader_transactions.csv"])
def test_backtrader_transactions_are_filled_market_orders(root, tmp_path, name):
    _, rows = _run(root, tmp_path, "backtrader", name)
    assert [(r["side"], r["type"], r["size"], r["fill_price"], r["filled"]) for r in rows] == [
        ("buy", "market", "0.1", "69867.95", "1"), ("sell", "market", "0.1", "69897.15", "1")]
    assert rows[0]["ts"] == str(T0 + 4 * 60_000)


def test_vectorbt_split_by_column(root, tmp_path):
    out = tmp_path / "vbt.csv"
    assert cli.main(["import", "vectorbt", str(root / FIX / "vectorbt_orders.csv"), "-o", str(out), "--split"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["vbt.BTCUSDT.csv", "vbt.ETHUSDT.csv"]


def test_symbol_filter(root, tmp_path):
    _, rows = _run(root, tmp_path, "vectorbt", "vectorbt_orders.csv", "--symbol", "ETHUSDT")
    assert [r["symbol"] for r in rows] == ["ETHUSDT"]


def test_unknown_symbol_is_an_error(root, tmp_path, capsys):
    rc = cli.main(["import", "vectorbt", str(root / FIX / "vectorbt_orders.csv"), "-o", str(tmp_path / "x.csv"),
                   "--symbol", "SOLUSDT"])
    assert rc == cli.EXIT_ERROR and "no orders for symbol" in capsys.readouterr().err


def test_freqtrade_backtest_result(root, tmp_path):
    _, rows = _run(root, tmp_path, "freqtrade", "freqtrade_backtest.json")
    entry, exit_ = rows
    assert (entry["symbol"], entry["side"], entry["type"], entry["price"], entry["pnl"]) == \
        ("BTC/USDT:USDT", "buy", "limit", "69924.7", "")
    assert (exit_["side"], exit_["type"], exit_["fill_price"], exit_["pnl"]) == ("sell", "market", "69950.1", "0.254")


def test_freqtrade_orders_pair_from_dry_run_id(root, tmp_path):
    _, rows = _run(root, tmp_path, "freqtrade", "freqtrade_orders.csv", "--symbol", "BTC/USDT:USDT")
    assert [(r["side"], r["filled"]) for r in rows] == [("buy", "1"), ("sell", "0")]
    assert rows[0]["end_ts"] == str(T0 + 11_234) and rows[1]["end_ts"] == str(T0 + 30_000)


def test_freqtrade_pair_parse():
    assert importers._ft_pair_from_id("dry_run_buy_BTC/USDT:USDT_1711756860.0") == "BTC/USDT:USDT"
    assert importers._ft_pair_from_id("dry_run_buyBTC/USDT:USDTe1b2c3d4-aaaa") == "BTC/USDT:USDT"


def test_hummingbot_fills_fold_per_order(root, tmp_path):
    _, rows = _run(root, tmp_path, "hummingbot", "hummingbot_trades.csv")
    o = _by_id(rows)
    b = o["buy-BTC-USDT-1"]
    assert (b["type"], b["size"], b["filled"], b["ts"], b["end_ts"]) == \
        ("limit", "0.01", "1", str(T0 + 11_234), str(T0 + 12_000))
    assert o["sell-BTC-USDT-2"]["type"] == "market"


def test_timestamp_forms():
    assert importers.to_ms("2024-03-30T00:01:00Z") == T0
    assert importers.to_ms("2024-03-30 00:01:00.000000001+00:00") == T0
    assert importers.to_ms("2024-03-30 00:01:00") == T0  # naive = UTC
    assert importers.to_ms(T0 / 1000) == T0 and importers.to_ms(T0 * 1_000_000) == T0
    assert importers.to_ms("") is None


def test_both_checks_read_the_output(root, tmp_path):
    """The point of the schema: fill-gap and cost-gap take the imported log as is."""
    pytest.importorskip("pandas")
    from backtest2live.costgap import engine as cg
    from backtest2live.fillgap import data

    out, _ = _run(root, tmp_path, "nautilus", "nautilus_orders_report.csv")
    resting = data.load_orders(str(out), 60.0, log=lambda m: None)
    assert len(resting["t"]) == 2 and resting["claimed"].tolist() == [True, False]
    takers = cg.load_orders(str(out), log=lambda m: None)
    assert len(takers["t"]) == 3 and takers["has_fill_px"]
