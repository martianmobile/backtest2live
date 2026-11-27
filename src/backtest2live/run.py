"""backtest2live · run — a local backtest for a strategy written against the published contract.

    bt2live run my_strategy --pair BTC-USDT --days 1

`my_strategy` is a package or a single module that registers a strategy with
`@register_strategy(config=...)` from `trading-engine-interface`. The strategy
is constructed once with its config; each `decide()` call receives market data,
its position, its open orders and its balances, and returns orders.

The run happens on your machine. The only network calls are the public candle
downloads from the exchange you name; nothing is sent to us.

The simulator is `replay.py` in this package: a candle replay with no queue,
which is the optimistic end of the fill band. Where the real fill rate sits is
what `bt2live fill-gap` estimates and what only live fills settle.

Needs Python 3.12 and the `run` extra: pip install 'backtest2live[run]'.
"""

import importlib
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

EXTRA_HINT = "bt2live run needs Python 3.12+ and the run extra: pip install 'backtest2live[run]'"
VENUES = ("binance", "okx")
OPTIMISTIC_NOTE = (
    "Fill model: candle replay. A resting order fills in full when a later candle reaches its price, with\n"
    "no queue. This is the optimistic end of the fill band; the real fill rate is lower.\n"
    "`bt2live fill-gap` estimates the gap from an order log."
)


def add_arguments(p):
    p.add_argument("package", help="Importable package or module that registers your strategy (e.g. my_strategy)")
    p.add_argument("--path", default=".", help="Directory that contains the package (default: current directory)")
    p.add_argument("--strategy", help="Strategy class name. Needed only when the package registers more than one")
    p.add_argument("--pair", required=True, help="Pair to trade, as BASE-QUOTE (e.g. BTC-USDT). Comma-separate several")
    p.add_argument("--venue", choices=VENUES, default="okx",
                   help="Exchange whose public candles are replayed (default: okx)")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--days", type=int, help="Backtest the last N days")
    when.add_argument("--from", dest="start", metavar="DATE", help="Start, YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (UTC)")
    p.add_argument("--to", dest="end", metavar="DATE", help="End, same format. Required with --from")
    p.add_argument("--capital", default="100000", help="Starting cash in the quote asset (default: 100000)")
    p.add_argument("--taker-fee-bps", type=int, default=10, help="Taker fee in basis points (default: 10)")
    p.add_argument("--maker-fee-bps", type=int, default=8,
                   help="Maker fee in basis points (default: 8). Negative is a rebate")
    p.add_argument("--slippage-bps", type=int, default=5, help="Slippage on taker fills in basis points (default: 5)")
    p.add_argument("--spread-bps", type=int, default=10,
                   help="Width of the order book built around each candle close, in basis points (default: 10)")
    p.add_argument("--run-interval", type=float, default=5.0,
                   help="Seconds between decide() calls written into the config (default: 5). The replay itself "
                        "calls decide() once per candle")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="Override a field of your strategy's config. Repeatable; values are parsed as JSON")
    p.add_argument("--export-orders", metavar="PATH", help="Write every order and what happened to it, as JSON")
    p.add_argument("--verbose", action="store_true", help="Also show why decide() returned no orders")


def _fail(message):
    raise SystemExit(f"error: {message}")


def _when(text, flag):
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    _fail(f"{flag} {text!r} is not a date. Use YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")


def _window(args):
    if args.days is not None:
        if args.days < 1:
            _fail("--days must be at least 1")
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        return end - timedelta(days=args.days), end
    if not args.end:
        _fail("--from needs --to")
    start, end = _when(args.start, "--from"), _when(args.end, "--to")
    if end <= start:
        _fail("--to must be after --from")
    return start, end


def _overrides(items):
    parsed = {}
    for item in items:
        key, sep, raw = item.partition("=")
        if not sep or not key.strip():
            _fail(f"--set {item!r} is not KEY=VALUE")
        try:
            parsed[key.strip()] = json.loads(raw)
        except json.JSONDecodeError:
            parsed[key.strip()] = raw  # a bare word is a string
    return parsed


def _load(package, path):
    """Import the simulator and the user's package. Returns (replay module, strategy registry)."""
    if sys.version_info < (3, 12):
        _fail(EXTRA_HINT + f" (this is Python {sys.version_info.major}.{sys.version_info.minor})")
    root = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(root):
        _fail(f"--path {path!r} is not a directory")
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        found = importlib.util.find_spec(package)
    except (ImportError, ValueError) as e:
        _fail(f"cannot import {package!r}: {e}")
    if found is None:
        _fail(f"no package or module named {package!r} in {root} or on the Python path")
    try:
        replay = importlib.import_module("backtest2live.replay")
        registry = importlib.import_module("trading_engine_interface.runtime").STRATEGY_REGISTRY
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] in ("trading_engine_interface", "httpx", "pydantic"):
            _fail(EXTRA_HINT)
        raise
    importlib.import_module(package)  # importing it runs @register_strategy
    return replay, registry


def _pick_strategy(registry, package, wanted):
    mine = sorted(e.name for e in registry.list_entries() if e.module == package or e.module.startswith(package + "."))
    if wanted:
        if wanted not in mine:
            _fail(f"{package!r} registers no strategy named {wanted!r}. It registers: {', '.join(mine) or 'none'}")
        return wanted
    if not mine:
        _fail(f"{package!r} registers no strategy. Decorate the class with @register_strategy(config=YourConfig)")
    if len(mine) > 1:
        _fail(f"{package!r} registers {len(mine)} strategies ({', '.join(mine)}). Pick one with --strategy")
    return mine[0]


def _symbols(text):
    from trading_engine_interface import Symbol

    symbols = []
    for pair in (p.strip().upper() for p in text.split(",") if p.strip()):
        try:
            symbols.append(Symbol(pair))
        except ValueError:
            _fail(f"--pair {pair!r} is not a pair the contract knows. It knows: {', '.join(s.value for s in Symbol)}")
    if not symbols:
        _fail("--pair is empty")
    return symbols


def run(args):
    start, end = _window(args)
    overrides = _overrides(args.overrides)
    try:
        capital = Decimal(args.capital)
    except InvalidOperation:
        _fail(f"--capital {args.capital!r} is not a number")
    if capital <= 0:
        _fail("--capital must be positive")

    replay, registry = _load(args.package, args.path)
    strategy = _pick_strategy(registry, args.package, args.strategy)
    symbols = _symbols(args.pair)
    try:
        config = replay.build_config(registry, strategy, symbols, capital, args.run_interval, overrides)
        candles = replay.download(args.venue, config, start, end)
        result = replay.replay(registry[strategy], config, candles, start, end, capital, args.taker_fee_bps,
                               args.maker_fee_bps, args.slippage_bps, args.spread_bps)
    except replay.ReplayError as e:
        _fail(str(e))

    print(replay.report(result, args.venue, verbose=args.verbose))
    if args.export_orders:
        with open(args.export_orders, "w", encoding="utf-8") as f:
            json.dump(result.orders, f, indent=2)
        print(f"Orders written to {args.export_orders}")
    print()
    print(OPTIMISTIC_NOTE)
    return 0
