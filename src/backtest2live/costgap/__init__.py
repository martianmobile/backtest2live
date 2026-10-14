"""backtest2live · cost-gap — what your backtest's taker orders would have paid on the real book.

Re-prices each market order, and each limit order that crosses the spread,
against top-of-book at its arrival time (plus latency) and reports the cost
against the price the backtest booked. The check for bar-based backtests,
where every fill is a market order at a bar price.

numpy and pandas are imported inside run(), so `bt2live convergence` keeps
working on a bare Python install.
"""

import sys

SCHEMA = "backtest2live.cost-gap/1"
VENUES = ("binance-um",)
CTA_URL = "https://martianmobile.com/fill-autopsy?utm_source=oss&utm_medium=cli&utm_campaign=cost-gap"
VERDICT_EXIT = {"CONSISTENT": 0, "REPORTED": 0, "OVERSTATED": 1}
LATENCY_SWEEP_MS = (0.0, 5.0, 20.0, 100.0)


def add_arguments(p):
    p.add_argument("orders", help="Your backtest's order log (CSV): time, side, size, and optionally type "
                                  "(market/limit), price (the limit; with no type or fill_price, the booked fill), "
                                  "fill_price (what the backtest booked), pnl.")
    src = p.add_argument_group("market data (pick one)")
    src.add_argument("--venue", choices=VENUES,
                     help="Download public data: binance-um = Binance USDT-M futures archive "
                          "(bookTicker ends 2024-03-30)")
    src.add_argument("--pair", help="Symbol for --venue, e.g. BTCUSDT")
    src.add_argument("--book", help="Your own top-of-book CSV: ts, bid_px, bid_qty, ask_px, ask_qty")
    p.add_argument("--latency-ms", type=float, default=0.0,
                   help="Delay each order's arrival at the book by this much (default 0); the report "
                        "also shows the mean cost at 0, 5, 20 and 100 ms")
    p.add_argument("--tick", type=float, help="Price tick (inferred from the book if omitted)")
    p.add_argument("--tolerance", type=float, default=0.25,
                   help="Max share of the backtest's P&L the execution cost may remove before the "
                        "verdict is OVERSTATED (default 0.25; needs a pnl column)")
    p.add_argument("--offline", action="store_true", help="Use cached data only; never download")
    p.add_argument("--run-name", help="Label for the report header")
    p.add_argument("--json", action="store_true", help="Print a machine-readable result instead of Markdown")


def run(args):
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        raise SystemExit("error: cost-gap needs numpy and pandas: pip install 'backtest2live[data]'")
    from backtest2live.costgap import engine, report

    res = engine.evaluate(args, log=lambda m: print(m, file=sys.stderr))
    if args.json:
        import json
        print(json.dumps(report.to_json(res), indent=2))
    else:
        print(report.markdown(res))
    return VERDICT_EXIT[res["verdict"]]
