"""The candle-replay simulator behind `bt2live run`. Needs Python 3.12+ and the run extra."""

import json
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

if sys.version_info < (3, 12):
    pytest.skip("bt2live run needs Python 3.12+", allow_module_level=True)
pytest.importorskip("trading_engine_interface", reason="needs the run extra")

import trading_engine_interface.common.utils as iface_utils  # noqa: E402
from trading_engine_interface import (  # noqa: E402
    BaseStrategyConfig,
    CancelOrderRequest,
    CreateOrderRequest,
    Kline,
    OrderType,
    PositionParams,
    Side,
    StrategyOutput,
    Symbol,
    TimeInterval,
)
from trading_engine_interface import now as utc_now  # noqa: E402  (a strategy module imports it the same way)

from backtest2live import cli, replay  # noqa: E402

BTC = Symbol.BTC_USDT
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
FIVE = timedelta(minutes=5)
# No slippage and a 2 bps book: at a close of 100 the touch is 99.99 / 100.01.
COSTS = dict(taker_fee_bps=10, maker_fee_bps=8, slippage_bps=0, spread_bps=2)


class Config(BaseStrategyConfig):
    class_type: str = "Scripted"
    position: PositionParams = PositionParams()
    quantity: Decimal = Decimal("1")


class Registry:
    def get_config_class(self, name):
        if name != "Scripted":
            raise ValueError(name)
        return Config


def candles(*rows):
    """Five-minute candles from (low, high, close) rows, the first one closing at T0 + 5m."""
    return {BTC: {TimeInterval.MIN_5: [
        Kline(venue=replay.VENUE, symbol=BTC, interval=TimeInterval.MIN_5, open_time=T0 + i * FIVE,
              close_time=T0 + (i + 1) * FIVE - timedelta(milliseconds=1), open=Decimal(close), high=Decimal(high),
              low=Decimal(low), close=Decimal(close), base_volume=Decimal(1), quote_volume=Decimal(close),
              number_of_trades=0)
        for i, (low, high, close) in enumerate(rows)
    ]}}


def order(side, order_type, price="100", quantity="1", trigger=None):
    return CreateOrderRequest(venue=replay.VENUE, symbol=BTC, order_type=order_type, side=side,
                              quantity=Decimal(quantity), price=Decimal(price),
                              trigger_price=None if trigger is None else Decimal(trigger))


def scripted(script):
    """A strategy that returns script[n](input) on its n-th decide() call and nothing otherwise."""

    class Scripted:
        def __init__(self, config):
            self.calls = 0

        def decide(self, input_data, predictions=None):
            step = script.get(self.calls)
            self.calls += 1
            return step(input_data) if step else StrategyOutput()

    return Scripted


def run(script, rows, capital="1000", **costs):
    config = replay.build_config(Registry(), "Scripted", [BTC], Decimal(capital), 5.0, {})
    data = candles(*rows)
    return replay.replay(scripted(script), config, data, T0, T0 + len(rows) * FIVE, Decimal(capital), **{**COSTS, **costs})


FLAT = ("100", "100", "100")


def test_a_resting_buy_fills_at_its_own_price_when_a_later_candle_reaches_it():
    result = run({0: lambda _: StrategyOutput(create_orders=[order(Side.BUY, OrderType.LIMIT, "99")])},
                 [FLAT, ("99.5", "100", "100"), ("98", "100", "100")])
    (placed,) = result.orders
    assert placed["status"] == "filled" and placed["liquidity"] == "MAKER"
    assert placed["fill_price"] == "99"  # its own price, though the candle traded down to 98
    assert placed["closed_at"] == (T0 + 3 * FIVE - timedelta(milliseconds=1)).isoformat()  # third candle, not second
    assert Decimal(placed["fee"]) == Decimal("99") * Decimal("0.0008")


def test_an_order_is_not_filled_by_the_candle_it_was_placed_on():
    # The first candle traded at 90, but the order was placed at its close.
    result = run({0: lambda _: StrategyOutput(create_orders=[order(Side.BUY, OrderType.LIMIT, "95")])},
                 [("90", "100", "100"), FLAT])
    assert result.orders[0]["status"] == "open" and not result.fills


def test_a_market_order_fills_at_the_far_touch_plus_slippage_and_pays_the_taker_fee():
    result = run({0: lambda _: StrategyOutput(create_orders=[order(Side.BUY, OrderType.MARKET)])}, [FLAT], slippage_bps=10)
    (fill,) = result.fills
    assert fill.price == Decimal("100.01") * Decimal("1.001")
    assert fill.fee == fill.price * Decimal("0.001")
    assert result.orders[0]["liquidity"] == "TAKER"


def test_a_crossing_limit_never_fills_worse_than_its_price():
    result = run({0: lambda _: StrategyOutput(create_orders=[order(Side.BUY, OrderType.LIMIT, "100.02")])},
                 [FLAT], slippage_bps=50)
    assert result.fills[0].price == Decimal("100.02")


@pytest.mark.parametrize("request_,status,reason", [
    (order(Side.BUY, OrderType.POST_ONLY, "101"), "rejected", "post-only order would cross the book"),
    (order(Side.BUY, OrderType.IOC, "99"), "canceled", "did not cross the book"),
    (order(Side.SELL, OrderType.STOP_LOSS, trigger="99"), "rejected", "stop orders are not simulated"),
    (order(Side.BUY, OrderType.MARKET, quantity="11"), "rejected", "not enough cash"),
])
def test_orders_that_cannot_fill_are_logged_with_the_reason(request_, status, reason):
    result = run({0: lambda _: StrategyOutput(create_orders=[request_])}, [FLAT, FLAT])
    assert (result.orders[0]["status"], result.orders[0]["reason"]) == (status, reason)
    assert not result.fills and result.equity == Decimal("1000")
    assert result.not_simulated == ({"stop orders": 1} if "stop" in reason else {})


def test_a_canceled_order_no_longer_fills_and_the_strategy_sees_its_open_orders():
    seen = []

    def cancel(input_data):
        seen.extend(input_data.open_orders)
        return StrategyOutput(cancel_orders=[CancelOrderRequest(order_id=input_data.open_orders[0].id)])

    result = run({0: lambda _: StrategyOutput(create_orders=[order(Side.BUY, OrderType.LIMIT, "99")]), 1: cancel},
                 [FLAT, FLAT, ("90", "100", "100")])
    assert [(o.price, o.side) for o in seen] == [(Decimal("99"), Side.BUY)]
    assert result.orders[0]["status"] == "canceled" and not result.fills


def test_pnl_is_the_cash_flow_of_the_fills_plus_the_open_position_at_the_last_close():
    result = run({0: lambda _: StrategyOutput(create_orders=[order(Side.BUY, OrderType.MARKET, quantity="2")]),
                  1: lambda _: StrategyOutput(create_orders=[order(Side.SELL, OrderType.MARKET)])},
                 [FLAT, ("110", "110", "110"), ("120", "120", "120")])
    cash_flow = sum(f.quote_amount for f in result.fills)
    assert result.pnl == cash_flow + Decimal("1") * Decimal("120")
    assert result.positions[BTC].base_amount == Decimal("1")
    assert result.realized > 0 and result.pnl - result.realized > 0  # one unit sold higher, one still held higher


def test_the_strategy_is_shown_cash_and_holdings_not_a_starting_inventory():
    balances = []
    run({0: lambda i: balances.append(i.balances) or StrategyOutput(create_orders=[order(Side.BUY, OrderType.MARKET)]),
         1: lambda i: balances.append(i.balances) or StrategyOutput()}, [FLAT, FLAT])
    before, after = (b[replay.VENUE] for b in balances)
    assert {a.value: b.free for a, b in before.items()} == {"USDT": Decimal("1000"), "BTC": Decimal("0")}
    assert after[BTC.base_asset].free == Decimal("1") and after[BTC.quote_asset].free < Decimal("900")


def test_the_contract_clock_follows_the_candles_and_is_restored():
    times = []
    run({0: lambda _: times.append(utc_now()) or StrategyOutput(),
         1: lambda _: times.append(utc_now()) or StrategyOutput()}, [FLAT, FLAT])
    assert times == [T0 + FIVE - timedelta(milliseconds=1), T0 + 2 * FIVE - timedelta(milliseconds=1)]
    assert utc_now is iface_utils.now and utc_now() > datetime(2026, 6, 1, tzinfo=timezone.utc)  # wall clock again


def test_an_async_decide_is_awaited():
    class Async:
        def __init__(self, config):
            pass

        async def decide(self, input_data, predictions=None):
            return StrategyOutput(create_orders=[order(Side.BUY, OrderType.MARKET)])

    config = replay.build_config(Registry(), "Scripted", [BTC], Decimal(1000), 5.0, {})
    result = replay.replay(Async, config, candles(FLAT), T0, T0 + FIVE, Decimal(1000), **COSTS)
    assert len(result.fills) == 1


def test_config_is_the_strategys_own_class_with_set_values_applied():
    config = replay.build_config(Registry(), "Scripted", [BTC], Decimal(1000), 2.5, {"quantity": 3})
    assert type(config) is Config and config.quantity == Decimal(3) and config.runtime.run_interval == 2.5
    assert config.symbol_to_venues == {BTC: [replay.VENUE]}


@pytest.mark.parametrize("name,overrides,message", [
    ("Scripted", {"nope": 1}, "--set names no field of Config: nope"),
    ("Scripted", {"quantity": "many"}, "could not build Config"),
    ("Unregistered", {}, "Register it with @register_strategy(config=YourConfig)"),
])
def test_config_errors_say_what_to_fix(name, overrides, message):
    with pytest.raises(replay.ReplayError, match=message.replace("(", r"\(").replace(")", r"\)")):
        replay.build_config(Registry(), name, [BTC], Decimal(1000), 5.0, overrides)


def test_no_candles_in_the_window_is_an_error_not_an_empty_result():
    config = replay.build_config(Registry(), "Scripted", [BTC], Decimal(1000), 5.0, {})
    with pytest.raises(replay.ReplayError, match="no 5m candles for BTC-USDT"):
        replay.replay(scripted({}), config, candles(FLAT), T0 + timedelta(days=1), T0 + timedelta(days=2), Decimal(1000), **COSTS)


def test_okx_pages_are_walked_backwards_and_returned_oldest_first(monkeypatch):
    start_ms = int(T0.timestamp() * 1000)
    rows = [[str(start_ms + i * 300_000), "1", "2", "0.5", "1.5", "10", "15", "15", "1"] for i in range(150)]
    calls = []

    def get(client, url, params):
        calls.append(params)
        older = [r for r in reversed(rows) if int(r[0]) < int(params["after"])]
        return {"code": "0", "data": older[:100]}

    monkeypatch.setattr(replay, "_get", get)
    monkeypatch.setattr(replay.time, "sleep", lambda s: None)
    got = replay.fetch_candles("okx", BTC, TimeInterval.MIN_5, T0 + 10 * FIVE, T0 + 150 * FIVE)
    assert len(calls) == 2 and [c.open_time for c in got] == [T0 + i * FIVE for i in range(10, 150)]
    assert (got[0].low, got[0].high, got[0].close, got[0].quote_volume) == (Decimal("0.5"), 2, Decimal("1.5"), 15)


def test_binance_pages_are_walked_forwards(monkeypatch):
    start_ms = int(T0.timestamp() * 1000)
    rows = [[start_ms + i * 300_000, "1", "2", "0.5", "1.5", "10", 0, "15", 7] for i in range(1200)]
    calls = []

    def get(client, url, params):
        calls.append(params)
        assert params["symbol"] == "BTCUSDT"
        return [r for r in rows if r[0] >= params["startTime"]][:1000]

    monkeypatch.setattr(replay, "_get", get)
    got = replay.fetch_candles("binance", BTC, TimeInterval.MIN_5, T0, T0 + 1200 * FIVE)
    assert len(calls) == 2 and [c.open_time for c in got] == [T0 + i * FIVE for i in range(1200)]
    assert (got[0].close, got[0].quote_volume) == (Decimal("1.5"), 15)


def test_a_location_block_names_the_way_out(monkeypatch):
    import httpx

    def blocked(request):
        return httpx.Response(451, request=request)

    with httpx.Client(transport=httpx.MockTransport(blocked)) as client:
        with pytest.raises(replay.ReplayError, match="api.binance.com does not serve candles to this location"):
            replay._get(client, "https://api.binance.com/api/v3/klines", {})


def test_the_command_runs_the_installed_example_and_exports_its_orders(monkeypatch, tmp_path, capsys):
    rows = [FLAT, ("99", "100", "99.5"), ("98", "100", "99")]
    monkeypatch.setattr(replay, "download", lambda source, config, start, end: candles(*rows))
    out_file = tmp_path / "orders.json"
    rc = cli.main(["run", "trading_engine_interface.examples.touch_bid_strategy", "--pair", "BTC-USDT",
                   "--from", "2026-01-01", "--to", "2026-01-02", "--export-orders", str(out_file), "--verbose"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Strategy      TouchBidStrategy on BTC-USDT" in out and "3 decide() calls" in out
    assert "2 filled" in out and "1 still open" in out and "(2 maker, 0 taker)" in out
    assert "optimistic end of the fill band" in out
    assert [o["status"] for o in json.loads(out_file.read_text())] == ["filled", "filled", "open"]


@pytest.mark.parametrize("argv,message", [
    (["--pair", "BTC-EUR", "--days", "1"], "'BTC-EUR' is not a pair the contract knows"),
    (["--pair", "BTC-USDT", "--days", "1", "--set", "nope=1"], "--set names no field of TouchBidConfig: nope"),
    (["--pair", "BTC-USDT", "--days", "1", "--strategy", "Other"], "registers no strategy named 'Other'"),
])
def test_bad_input_is_an_error_before_any_download(monkeypatch, capsys, argv, message):
    monkeypatch.setattr(replay, "download", lambda *a: pytest.fail("downloaded before validating"))
    assert cli.main(["run", "trading_engine_interface.examples.touch_bid_strategy", *argv]) == cli.EXIT_ERROR
    assert message in capsys.readouterr().err
