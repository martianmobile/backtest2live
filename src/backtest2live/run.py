"""backtest2live · run — a local backtest for a strategy written against the published contract.

    bt2live run my_strategy --pair BTC-USDT --days 1

`my_strategy` is a package or a single module that registers a strategy with
`@register_strategy(config=...)` from `trading-engine-interface`. The strategy
is constructed once with its config; each `decide()` call receives market data,
its position, its open orders and its balances, and returns orders.

The run happens on your machine. The only network calls are the public candle
downloads from the exchange you name; nothing is sent to us.

This first release replays candles. Every order fills when the next candle
allows it, with no queue: that is the optimistic end of the fill band. Where
the real fill rate sits is what `bt2live fill-gap` estimates and what only
live fills settle.

Needs Python 3.12 and the `run` extra: pip install 'backtest2live[run]'.
"""

import importlib
import importlib.util
import logging
import os
import sys

EXTRA_HINT = "bt2live run needs Python 3.12+ and the run extra: pip install 'backtest2live[run]'"
VENUES = {"okx": "OKX", "binance": "BINANCE"}
OPTIMISTIC_NOTE = (
    "Fill model: candle replay, every order fills with no queue. This is the optimistic end of the\n"
    "fill band; the real fill rate is lower. `bt2live fill-gap` estimates the gap from an order log."
)


def add_arguments(p):
    p.add_argument("package", help="Importable package or module that registers your strategy (e.g. my_strategy)")
    p.add_argument("--path", default=".", help="Directory that contains the package (default: current directory)")
    p.add_argument("--strategy", help="Strategy class name. Needed only when the package registers more than one")
    p.add_argument("--pair", required=True, help="Pair to trade, as BASE-QUOTE (e.g. BTC-USDT). Comma-separate several")
    p.add_argument("--venue", choices=sorted(VENUES), default="okx",
                   help="Exchange whose public candles are replayed (default: okx)")
    when = p.add_mutually_exclusive_group(required=True)
    when.add_argument("--days", type=int, help="Backtest the last N days")
    when.add_argument("--from", dest="start", metavar="DATE", help="Start, YYYY-MM-DD or 'YYYY-MM-DD HH:MM' (UTC)")
    p.add_argument("--to", dest="end", metavar="DATE", help="End, same format. Required with --from")
    p.add_argument("--capital", type=float, default=100000, help="Starting capital in the quote asset (default: 100000)")
    p.add_argument("--taker-fee-bps", type=int, default=10, help="Taker fee in basis points (default: 10)")
    p.add_argument("--maker-fee-bps", type=int, default=8,
                   help="Maker fee in basis points (default: 8). Negative is a rebate")
    p.add_argument("--slippage-bps", type=int, default=5, help="Slippage on taker fills in basis points (default: 5)")
    p.add_argument("--spread-bps", type=int, default=10,
                   help="Width of the order book built around each candle price, in basis points (default: 10)")
    p.add_argument("--run-interval", type=float, default=5.0,
                   help="Seconds between decide() calls written into the config (default: 5)")
    p.add_argument("--set", dest="overrides", action="append", default=[], metavar="KEY=VALUE",
                   help="Override a field of your strategy's config. Repeatable; values are parsed as JSON")
    p.add_argument("--export-orders", metavar="PATH", help="Write the run's orders to this file as JSON")
    p.add_argument("--verbose", action="store_true",
                   help="Show the runner's log and, at the end, why decide() returned no orders")


def _fail(message):
    raise SystemExit(f"error: {message}")


def _load_runner(package, path):
    """Import the runner with the user's package discovered. Returns (runner module, registry)."""
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
    # The runner discovers strategies when it is imported, from these two names.
    os.environ["STRATEGY_LOCAL_PATH"] = root
    os.environ["STRATEGY_PACKAGE"] = package
    try:
        runner = importlib.import_module("trading_engine_backtest.run")
        registry = importlib.import_module("trading_engine_interface.runtime").STRATEGY_REGISTRY
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] in ("trading_engine_backtest", "trading_engine_interface"):
            _fail(EXTRA_HINT)
        raise
    return runner, registry


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


def _runner_argv(args, strategy):
    argv = [
        "bt2live run",
        "--strategy", strategy,
        "--symbols", args.pair,
        "--kline-venue", VENUES[args.venue],
        "--capital", str(args.capital),
        "--taker-fee", str(args.taker_fee_bps),
        "--maker-fee", str(args.maker_fee_bps),
        "--slippage", str(args.slippage_bps),
        "--spread", str(args.spread_bps),
        "--run-interval", str(args.run_interval),
    ]
    if args.days is not None:
        argv += ["--days", str(args.days)]
    else:
        argv += ["--start", args.start, "--end", args.end]
    for item in args.overrides:
        argv += ["--set", item]
    if args.export_orders:
        argv += ["--export-orders", args.export_orders]
    if args.verbose:
        argv.append("--verbose")
    return argv


class _Tee:
    """Pass the runner's report through and keep it, to read its final status."""

    def __init__(self, stream):
        self._stream = stream
        self.text = ""

    def write(self, data):
        self.text += data
        return self._stream.write(data)

    def flush(self):
        self._stream.flush()


def run(args):
    if args.days is not None and args.days < 1:
        _fail("--days must be at least 1")
    if args.start and not args.end:
        _fail("--from needs --to")
    if args.end and not args.start:
        _fail("--to needs --from")

    runner, registry = _load_runner(args.package, args.path)
    strategy = _pick_strategy(registry, args.package, args.strategy)
    if not args.verbose:
        # The runner logs every step at INFO; keep the report readable.
        logging.getLogger().setLevel(logging.ERROR)

    saved_argv, saved_stdout = sys.argv, sys.stdout
    seen = _Tee(saved_stdout)
    sys.argv, sys.stdout = _runner_argv(args, strategy), seen
    try:
        runner.main()
    except SystemExit as e:
        # The runner exits on a bad value after printing why; that is a failed run, not a verdict.
        if e.code not in (0, None):
            _fail("the backtest did not start (see the message above)")
    finally:
        sys.argv, sys.stdout = saved_argv, saved_stdout
    if "Status: COMPLETED" not in seen.text:
        _fail("the backtest did not complete (see the output above)")
    print()
    print(OPTIMISTIC_NOTE)
    return 0
