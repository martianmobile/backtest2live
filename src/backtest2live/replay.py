"""backtest2live · replay — the candle-replay simulator behind `bt2live run`.

It is deliberately naive, and short enough to read in one sitting:

1. Download public candles for each pair.
2. At every candle close, build the same `StrategyInput` a live engine would
   (order book, candle history, position, open orders, balances) and call the
   strategy's `decide()`.
3. Fill what it returns by the rules below, and keep cash and positions with the
   position math published in `trading-engine-interface`.

Fill rules:

- The order book is one level each side, `--spread-bps` wide around the candle close.
- A market order, or a limit that crosses the book, fills at once at the far
  touch plus slippage, and pays the taker fee.
- A limit or post-only order that does not cross rests. It fills in full, at its
  own price, on the first later candle whose low (buy) or high (sell) reaches
  that price, and pays the maker fee. There is no queue and no partial fill:
  this is the optimistic end of the fill band.
- Stop orders, brackets, replaces and stop amendments are not simulated. They
  are counted and reported, never silently dropped.
- Risk limits in the config are not enforced.

This module needs Python 3.12 and `trading-engine-interface`; `run.py` imports
it only after checking for both.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Iterator, Optional

import httpx
import trading_engine_interface.common.utils as iface_utils
from trading_engine_interface import (
    AccountPermissionConfig,
    AccountType,
    Asset,
    Balance,
    BaseStrategyConfig,
    CreateOrderRequest,
    Fill,
    Kline,
    LiquidityType,
    Order,
    OrderBook,
    OrderStatus,
    OrderType,
    Position,
    PositionParams,
    RiskLimitConfig,
    RuntimeConfig,
    Side,
    StrategyInput,
    Symbol,
    TimeInterval,
    VenueName,
)
from trading_engine_interface.domain.fill_simulator import apply_slippage
from trading_engine_interface.domain.position_math import update_position
from trading_engine_interface.trading_engine.models.market_models import OrderBookEntry
from trading_engine_interface.trading_engine.models.position_models import PositionGroup
from trading_engine_interface.trading_engine.models.strategy_models import StrategyMarketData

# The strategy trades on one simulated venue, whichever exchange the candles came from.
VENUE = VenueName.BACKTEST
BOOK_DEPTH = Decimal("10000")
DEFAULT_INTERVAL = TimeInterval.MIN_5
DEFAULT_LOOKBACK = 200
STOP_TYPES = (OrderType.STOP_LOSS, OrderType.STOP_LOSS_LIMIT)

OKX_BARS = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m", "1h": "1H", "2h": "2H", "4h": "4H", "1d": "1D"}
BINANCE_BARS = ("1m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d")


class ReplayError(Exception):
    """The run cannot start or continue. The message is written for the user."""


# --- candles ---------------------------------------------------------------- #


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _get(client: httpx.Client, url: str, params: dict[str, Any]) -> Any:
    """GET with three tries. Public endpoints rate-limit; a short wait clears it."""
    for attempt in range(3):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as e:
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 451:
                raise ReplayError(f"{httpx.URL(url).host} does not serve candles to this location. Try the other --venue") from e
            if attempt == 2:
                raise ReplayError(f"could not download candles from {url}: {e}") from e
            time.sleep(1 + attempt)


def _candle(symbol: Symbol, interval: TimeInterval, open_ms: int, ohlcv: list[Any], quote_volume: Any) -> Kline:
    return Kline(
        venue=VENUE,
        symbol=symbol,
        interval=interval,
        open_time=_dt(open_ms),
        close_time=_dt(open_ms) + interval.timedelta - timedelta(milliseconds=1),
        open=Decimal(str(ohlcv[0])),
        high=Decimal(str(ohlcv[1])),
        low=Decimal(str(ohlcv[2])),
        close=Decimal(str(ohlcv[3])),
        base_volume=Decimal(str(ohlcv[4])),
        quote_volume=Decimal(str(quote_volume)),
        number_of_trades=0,
    )


def _okx_candles(client: httpx.Client, symbol: Symbol, interval: TimeInterval, start: datetime, end: datetime) -> list[Kline]:
    if interval.value not in OKX_BARS:
        raise ReplayError(f"OKX has no {interval.value} candles; it has {', '.join(OKX_BARS)}")
    rows: list[list[str]] = []
    cursor, start_ms = _ms(end), _ms(start)
    while True:  # OKX pages backwards from `after`, 100 candles at a time
        body = _get(
            client,
            "https://www.okx.com/api/v5/market/history-candles",
            {"instId": symbol.value, "bar": OKX_BARS[interval.value], "limit": "100", "after": str(cursor)},
        )
        if body.get("code") != "0":
            raise ReplayError(f"OKX refused the candle request for {symbol.value}: {body.get('msg')!r}")
        page = body.get("data", [])
        rows += [row for row in page if int(row[0]) >= start_ms]
        if len(page) < 100 or int(page[-1][0]) < start_ms or int(page[-1][0]) >= cursor:
            break
        cursor = int(page[-1][0])
        time.sleep(0.12)
    rows.sort(key=lambda row: int(row[0]))
    return [_candle(symbol, interval, int(r[0]), r[1:6], r[7] if len(r) > 7 else r[6]) for r in rows]


def _binance_candles(client: httpx.Client, symbol: Symbol, interval: TimeInterval, start: datetime, end: datetime) -> list[Kline]:
    if interval.value not in BINANCE_BARS:
        raise ReplayError(f"Binance has no {interval.value} candles; it has {', '.join(BINANCE_BARS)}")
    candles: list[Kline] = []
    cursor, end_ms = _ms(start), _ms(end)
    while cursor < end_ms:
        page = _get(
            client,
            "https://api.binance.com/api/v3/klines",
            {"symbol": symbol.value.replace("-", ""), "interval": interval.value, "startTime": cursor, "endTime": end_ms, "limit": 1000},
        )
        if not page:
            break
        candles += [_candle(symbol, interval, int(r[0]), r[1:6], r[7]) for r in page]
        if len(page) < 1000 or int(page[-1][0]) < cursor:
            break
        cursor = int(page[-1][0]) + 1
    return candles


SOURCES: dict[str, Callable[[httpx.Client, Symbol, TimeInterval, datetime, datetime], list[Kline]]] = {
    "okx": _okx_candles,
    "binance": _binance_candles,
}


def fetch_candles(source: str, symbol: Symbol, interval: TimeInterval, start: datetime, end: datetime) -> list[Kline]:
    """Closed candles from `start` to `end`, oldest first."""
    with httpx.Client(timeout=30.0) as client:
        candles = SOURCES[source](client, symbol, interval, start, end)
    return [c for c in candles if c.close_time <= end]


# --- config ----------------------------------------------------------------- #


def build_config(registry: Any, strategy: str, symbols: list[Symbol], capital: Decimal, run_interval: float,
                 overrides: dict[str, Any]) -> BaseStrategyConfig:
    """The strategy's own config class, filled with what a runner knows and the user's `--set` values."""
    try:
        config_cls = registry.get_config_class(strategy)
    except ValueError as e:
        raise ReplayError(f"{strategy} has no config class. Register it with @register_strategy(config=YourConfig)") from e
    unknown = sorted(set(overrides) - set(config_cls.model_fields))
    if unknown:
        raise ReplayError(f"--set names no field of {config_cls.__name__}: {', '.join(unknown)}. "
                          f"It has: {', '.join(sorted(config_cls.model_fields))}")
    values: dict[str, Any] = {
        "account_permission": AccountPermissionConfig(symbol_to_venues={s: [VENUE] for s in symbols}),
        # Sized to the capital so a strategy that reads its limits sees room to trade. Not enforced here.
        "risk_limit": RiskLimitConfig(max_total_loss=capital, max_drawdown_pct=100, max_order_value=capital,
                                      max_position_value=capital, max_capital_allocation_usd=capital,
                                      max_capital_allocation_pct=100, max_asset_usage_pct=100),
        "runtime": RuntimeConfig(run_interval=run_interval),
        "trading_symbols": symbols,
    }
    fields = config_cls.model_fields
    if fields["class_type"].is_required():
        values["class_type"] = strategy
    if fields["position"].is_required():
        values["position"] = PositionParams()
    try:
        config = config_cls(**values)
        if overrides:
            config = config_cls.model_validate({**config.model_dump(), **overrides})
    except ValueError as e:  # pydantic's ValidationError is a ValueError
        raise ReplayError(f"could not build {config_cls.__name__}: {e}") from e
    return config  # type: ignore[no-any-return]


# --- account ---------------------------------------------------------------- #


@dataclass
class Account:
    """Cash, positions and resting orders. Every order is logged with what happened to it."""

    capital: Decimal
    symbols: list[Symbol]
    taker_fee_bps: int
    maker_fee_bps: int
    slippage_bps: int
    cash: Decimal = field(init=False)
    positions: dict[Symbol, Position] = field(init=False)
    fills: list[Fill] = field(default_factory=list)
    resting: dict[int, CreateOrderRequest] = field(default_factory=dict)
    log: dict[int, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        quotes = {s.quote_asset for s in self.symbols}
        if len(quotes) != 1:
            raise ReplayError(f"all pairs must share one quote asset; got {', '.join(sorted(a.value for a in quotes))}")
        self.quote: Asset = quotes.pop()
        self.cash = self.capital
        self.positions = {s: Position(symbol=s) for s in self.symbols}

    def submit(self, order: CreateOrderRequest, book: OrderBook, at: datetime) -> None:
        order_id = len(self.log) + 1
        self.log[order_id] = {
            "id": order_id, "placed_at": at.isoformat(), "symbol": order.symbol.value, "side": order.side.value,
            "type": order.order_type.value, "quantity": str(order.quantity),
            "price": None if order.price is None else str(order.price), "status": "open",
        }
        if order.symbol not in self.positions:
            return self._end(order_id, "rejected", at, reason="pair is not in this run")
        if order.order_type in STOP_TYPES:
            return self._end(order_id, "rejected", at, reason="stop orders are not simulated")
        buy = order.side == Side.BUY
        touch = book.best_ask_price if buy else book.best_bid_price
        assert touch is not None  # the synthetic book always has both sides
        if order.order_type == OrderType.MARKET:
            return self._fill(order_id, order, apply_slippage(touch, order.side, self.slippage_bps), LiquidityType.TAKER, at)
        crosses = order.price >= touch if buy else order.price <= touch
        if order.order_type == OrderType.POST_ONLY and crosses:
            return self._end(order_id, "rejected", at, reason="post-only order would cross the book")
        if crosses:
            slipped = apply_slippage(touch, order.side, self.slippage_bps)
            price = min(slipped, order.price) if buy else max(slipped, order.price)  # never worse than the limit
            return self._fill(order_id, order, price, LiquidityType.TAKER, at)
        if order.order_type in (OrderType.IOC, OrderType.FOK):
            return self._end(order_id, "canceled", at, reason="did not cross the book")
        self.resting[order_id] = order

    def match(self, candle: Kline) -> None:
        """Fill every resting order this candle's range reached, at the order's own price."""
        for order_id, order in list(self.resting.items()):
            if order.symbol != candle.symbol:
                continue
            reached = candle.low <= order.price if order.side == Side.BUY else candle.high >= order.price
            if reached:
                del self.resting[order_id]
                self._fill(order_id, order, order.price, LiquidityType.MAKER, candle.close_time)

    def cancel(self, order_id: int, at: datetime) -> None:
        if self.resting.pop(order_id, None) is not None:
            self._end(order_id, "canceled", at, reason="canceled by the strategy")

    def _fill(self, order_id: int, order: CreateOrderRequest, price: Decimal, liquidity: LiquidityType, at: datetime) -> None:
        fee_bps = self.taker_fee_bps if liquidity == LiquidityType.TAKER else self.maker_fee_bps
        fee = order.quantity * price * Decimal(fee_bps) / Decimal(10000)
        needed = order.quantity * price + fee if order.side == Side.BUY else fee
        if needed > self.cash:
            return self._end(order_id, "rejected", at, reason="not enough cash")
        fill = Fill(
            venue=VENUE, symbol=order.symbol, side=order.side, quantity=order.quantity, price=price,
            liquidity_type=liquidity, timestamp=at, venue_user_id="backtest", account_type=AccountType.SPOT,
            fee=fee, fee_asset=self.quote, venue_fill_id=f"fill-{len(self.fills) + 1}",
            venue_order_id=f"order-{order_id}", strategy_id=1, order_id=order_id,
        )
        update_position(self.positions[order.symbol], fill)
        self.cash += fill.quote_amount  # signed, and net of the fee
        self.fills.append(fill)
        self._end(order_id, "filled", at, fill_price=str(price), liquidity=liquidity.value, fee=str(fee))

    def _end(self, order_id: int, status: str, at: datetime, **details: Any) -> None:
        self.log[order_id].update(status=status, closed_at=at.isoformat(), **details)

    # What the strategy is shown.

    def position_group(self, prices: dict[Symbol, Decimal]) -> PositionGroup:
        for symbol, position in self.positions.items():
            if symbol in prices:
                position.unrealized_pnl = position.base_amount * prices[symbol] + position.quote_amount
        return PositionGroup(strategy_id=1, positions=list(self.positions.values()), fills_count=len(self.fills),
                             fills_volume=sum((f.volume for f in self.fills), Decimal(0)))

    def open_orders(self) -> list[Order]:
        return [
            Order(id=order_id, venue=o.venue, symbol=o.symbol, order_type=o.order_type, side=o.side, quantity=o.quantity,
                  price=o.price, venue_user_id="backtest", account_type=AccountType.SPOT, status=OrderStatus.NEW,
                  client_order_id=o.client_order_id or f"order-{order_id}", strategy_id=1)
            for order_id, o in self.resting.items()
        ]

    def balances(self) -> dict[VenueName, dict[Asset, Balance]]:
        free = {self.quote: max(Decimal(0), self.cash)}
        for symbol, position in self.positions.items():
            free[symbol.base_asset] = max(Decimal(0), position.base_amount)
        return {VENUE: {asset: Balance(asset=asset, free=amount, venue=VENUE, venue_user_id="backtest",
                                       account_type=AccountType.SPOT) for asset, amount in free.items()}}

    def equity(self, prices: dict[Symbol, Decimal]) -> Decimal:
        return self.cash + sum((p.base_amount * prices[s] for s, p in self.positions.items() if s in prices), Decimal(0))


# --- clock ------------------------------------------------------------------ #


@contextlib.contextmanager
def simulated_clock() -> Iterator[Callable[[datetime], None]]:
    """Make the contract's `now()` return simulated time, so cooldowns and timeouts follow the candles.

    Yields a function that sets the time. Every module that imported `now` before
    the run starts is pointed at the simulated one and restored afterwards.
    """
    original = iface_utils.now
    current: list[Optional[datetime]] = [None]

    def simulated_now() -> datetime:
        return current[0] or original()

    # Under any name: `from trading_engine_interface import now as utc_now` is patched too.
    patched = [
        (module, name)
        for module in list(sys.modules.values())
        for name, value in list(getattr(module, "__dict__", {}).items())
        if value is original
    ]
    for module, name in patched:
        setattr(module, name, simulated_now)
    try:
        yield lambda value: current.__setitem__(0, value)
    finally:
        for module, name in patched:
            setattr(module, name, original)


# --- the run ---------------------------------------------------------------- #


@dataclass
class Result:
    strategy: str
    symbols: list[Symbol]
    start: datetime
    end: datetime
    interval: TimeInterval
    decisions: int
    capital: Decimal
    quote: Asset
    equity: Decimal
    realized: Decimal
    max_drawdown_pct: Decimal
    fills: list[Fill]
    orders: list[dict[str, Any]]
    positions: dict[Symbol, Position]
    prices: dict[Symbol, Decimal]
    skips: Counter[str]
    not_simulated: Counter[str]

    @property
    def pnl(self) -> Decimal:
        return self.equity - self.capital


def replay(strategy_cls: Any, config: BaseStrategyConfig, candles: dict[Symbol, dict[TimeInterval, list[Kline]]],
           start: datetime, end: datetime, capital: Decimal, taker_fee_bps: int, maker_fee_bps: int,
           slippage_bps: int, spread_bps: int) -> Result:
    """Run one strategy over candles already in memory. No network, no files."""
    symbols = list(config.symbols)
    intervals = _intervals(config)
    primary, lookback = intervals[0], _lookback(config)
    ticks = [c.close_time for c in candles[symbols[0]][primary] if c.close_time >= start]
    if not ticks:
        raise ReplayError(f"no {primary.value} candles for {symbols[0].value} between {start:%Y-%m-%d %H:%M} and {end:%Y-%m-%d %H:%M} UTC")

    account = Account(capital, symbols, taker_fee_bps, maker_fee_bps, slippage_bps)
    seen = {s: {i: 0 for i in intervals} for s in symbols}  # how many candles of each series have closed
    prices: dict[Symbol, Decimal] = {}
    skips: Counter[str] = Counter()
    not_simulated: Counter[str] = Counter()
    peak, max_drawdown = capital, Decimal(0)
    loop: Optional[asyncio.AbstractEventLoop] = None

    with simulated_clock() as set_time:
        set_time(ticks[0])
        strategy = strategy_cls(config)
        for tick in ticks:
            set_time(tick)
            books: dict[Symbol, dict[VenueName, OrderBook]] = {}
            history: dict[Symbol, dict[VenueName, dict[TimeInterval, list[Kline]]]] = {}
            for symbol in symbols:
                history[symbol] = {VENUE: {}}
                for interval in intervals:
                    series = candles[symbol].get(interval, [])
                    index = seen[symbol][interval]
                    while index < len(series) and series[index].close_time <= tick:
                        if interval == primary:
                            account.match(series[index])  # resting orders see the candle before the strategy does
                        index += 1
                    seen[symbol][interval] = index
                    history[symbol][VENUE][interval] = series[max(0, index - lookback):index]
                closed = history[symbol][VENUE][primary]
                if closed:
                    prices[symbol] = closed[-1].close
                    half = closed[-1].close * Decimal(spread_bps) / Decimal(20000)
                    books[symbol] = {VENUE: OrderBook(
                        venue=VENUE, symbol=symbol, timestamp=tick,
                        bids=[OrderBookEntry(price=closed[-1].close - half, quantity=BOOK_DEPTH)],
                        asks=[OrderBookEntry(price=closed[-1].close + half, quantity=BOOK_DEPTH)],
                    )}

            output = strategy.decide(
                StrategyInput(
                    market_data=StrategyMarketData(order_books=books, klines=history),
                    position_group=account.position_group(prices),
                    open_orders=account.open_orders(),
                    balances=account.balances(),
                ),
                predictions=None,
            )
            if inspect.isawaitable(output):
                loop = loop or asyncio.new_event_loop()
                output = loop.run_until_complete(output)

            if output.skip_reason is not None:
                skips[output.skip_reason.value] += 1
            for name in ("replace_orders", "bracket_orders", "amend_stops"):
                not_simulated[name] += len(getattr(output, name))
            for cancel in output.cancel_orders:
                account.cancel(cancel.order_id, tick)
            for order in output.create_orders:
                if order.symbol in books:
                    account.submit(order, books[order.symbol][VENUE], tick)

            equity = account.equity(prices)
            peak = max(peak, equity)
            if peak > 0:
                max_drawdown = max(max_drawdown, (peak - equity) / peak * 100)
    if loop is not None:
        loop.close()

    not_simulated["stop orders"] = sum(1 for o in account.log.values() if o.get("reason") == "stop orders are not simulated")
    return Result(
        strategy=config.class_type, symbols=symbols, start=start, end=end, interval=primary, decisions=len(ticks),
        capital=capital, quote=account.quote, equity=account.equity(prices),
        realized=sum((p.realized_pnl for p in account.positions.values()), Decimal(0)),
        max_drawdown_pct=max_drawdown, fills=account.fills, orders=list(account.log.values()),
        positions=account.positions, prices=prices, skips=skips, not_simulated=+not_simulated,
    )


def _intervals(config: BaseStrategyConfig) -> list[TimeInterval]:
    indicator = config.technical_indicator
    return list(indicator.intervals) if indicator and indicator.intervals else [DEFAULT_INTERVAL]


def _lookback(config: BaseStrategyConfig) -> int:
    return config.technical_indicator.min_klines_num if config.technical_indicator else DEFAULT_LOOKBACK


def download(source: str, config: BaseStrategyConfig, start: datetime, end: datetime) -> dict[Symbol, dict[TimeInterval, list[Kline]]]:
    """Every candle series the config asks for, each with enough history before `start` to fill its lookback."""
    lookback = _lookback(config)
    return {
        s: {i: fetch_candles(source, s, i, start - lookback * i.timedelta, end) for i in _intervals(config)}
        for s in config.symbols
    }


# --- report ----------------------------------------------------------------- #


def _money(value: Decimal) -> str:
    return f"{value:,.2f}"


def report(result: Result, source: str, verbose: bool = False) -> str:
    quote = result.quote.value
    count = Counter(o["status"] for o in result.orders)
    makers = sum(1 for f in result.fills if f.liquidity_type == LiquidityType.MAKER)
    volume = sum((f.volume for f in result.fills), Decimal(0))
    fees = sum((f.fee for f in result.fills), Decimal(0))
    lines = [
        f"Strategy      {result.strategy} on {', '.join(s.value for s in result.symbols)}",
        f"Window        {result.start:%Y-%m-%d %H:%M} to {result.end:%Y-%m-%d %H:%M} UTC",
        f"Candles       {source} {result.interval.value}, {result.decisions} decide() calls",
        f"Orders        {len(result.orders)} placed: {count['filled']} filled, {count['canceled']} canceled, "
        f"{count['rejected']} rejected, {count['open']} still open",
        f"Fills         {len(result.fills)} ({makers} maker, {len(result.fills) - makers} taker), "
        f"volume {_money(volume)} {quote}, fees {_money(fees)} {quote}",
        f"P&L           {_money(result.pnl)} {quote} ({result.pnl / result.capital * 100:+.2f}% of {_money(result.capital)})",
        f"              realized {_money(result.realized)}, open positions at the last close {_money(result.pnl - result.realized)}",
        f"Max drawdown  {result.max_drawdown_pct:.2f}%",
    ]
    for symbol, position in result.positions.items():
        if position.base_amount != 0:
            lines.append(f"Open          {position.base_amount} {symbol.base_asset.value}, last close {_money(result.prices[symbol])}")
    reasons = Counter(o["reason"] for o in result.orders if o["status"] == "rejected")
    for reason, n in reasons.most_common():
        lines.append(f"Rejected      {n}: {reason}")
    for name, n in result.not_simulated.items():
        if name != "stop orders":
            lines.append(f"Not simulated {n} {name.replace('_', ' ')} returned by decide() were ignored")
    if verbose and result.skips:
        lines.append("decide() returned no orders: " + ", ".join(f"{reason} x{n}" for reason, n in result.skips.most_common()))
    return "\n".join(lines)
