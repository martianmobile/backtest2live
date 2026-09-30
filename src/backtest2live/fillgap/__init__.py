"""backtest2live · fill-gap — how many of your backtest's passive fills survive a queue proxy.

Re-runs each resting order in your backtest's order log against public
top-of-book and trade prints, under a queue proxy: the order fills only after
the volume printed at its price covers the displayed queue ahead of it plus
its own size. Reports the backtest's fill rate against the proxy's.

numpy and pandas are imported inside run(), so `bt2live convergence` keeps
working on a bare Python install.
"""

import sys

SCHEMA = "backtest2live.fill-gap/1"
VENUES = ("binance-um",)
CTA_URL = "https://martianmobile.com/fill-autopsy?utm_source=oss&utm_medium=cli&utm_campaign=fill-gap"
VERDICT_EXIT = {"CONSISTENT": 0, "OVERSTATED": 1}


def add_arguments(p):
    p.add_argument("orders", help="Your backtest's order log (CSV): time, side, price, size, and the "
                                  "backtest's filled flag. See README for accepted column names.")
    src = p.add_argument_group("market data (pick one)")
    src.add_argument("--venue", choices=VENUES,
                     help="Download public data: binance-um = Binance USDT-M futures archive "
                          "(bookTicker ends 2024-03-30)")
    src.add_argument("--pair", help="Symbol for --venue, e.g. BTCUSDT")
    src.add_argument("--book", help="Your own top-of-book CSV: ts, bid_px, bid_qty, ask_px, ask_qty")
    src.add_argument("--trades", help="Your own trades CSV: ts, price, qty, is_buyer_maker (or taker side)")
    p.add_argument("--max-rest", type=float, default=60.0,
                   help="Seconds an order rests when the log has no end/cancel column (default 60)")
    p.add_argument("--latency-ms", type=float, default=0.0,
                   help="Delay each order's arrival at the book by this much (default 0)")
    p.add_argument("--tick", type=float, help="Price tick (inferred from the book if omitted)")
    p.add_argument("--tolerance", type=float, default=0.10,
                   help="Max share of claimed fills the proxy may miss before the verdict is "
                        "OVERSTATED (default 0.10)")
    p.add_argument("--offline", action="store_true", help="Use cached data only; never download")
    p.add_argument("--run-name", help="Label for the report header")
    p.add_argument("--json", action="store_true", help="Print a machine-readable result instead of Markdown")


def run(args):
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        raise SystemExit("error: fill-gap needs numpy and pandas: pip install 'backtest2live[data]'")
    from backtest2live.fillgap import engine, report

    res = engine.evaluate(args, log=lambda m: print(m, file=sys.stderr))
    if args.json:
        import json
        print(json.dumps(report.to_json(res), indent=2))
    else:
        print(report.markdown(res))
    return VERDICT_EXIT[res["verdict"]]
