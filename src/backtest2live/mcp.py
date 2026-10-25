"""backtest2live · mcp — the checks as tools for any AI assistant, over the Model Context Protocol.

`bt2live mcp` speaks MCP on stdio (newline-delimited JSON-RPC 2.0): a client
such as Claude Desktop or Cursor starts it as a subprocess. Tools:

    convergence   a variant sweep's verdict: CONVERGED / ITERATE / KILL
    fill_gap      a backtest's resting fills vs a queue proxy on market data
    cost_gap      a backtest's taker orders re-priced on the real book

Each tool returns the human report (Markdown) and the `--json` result. Files
are read from the machine the server runs on; the only network calls are the
public market-data downloads fill_gap and cost_gap already make with `venue`.
Nothing is sent to Martian Mobile. Pure standard library.
"""

import contextlib
import json
import math
import sys

from backtest2live import __version__

PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")  # newest first

_VENUE = {"type": "string", "enum": ["binance-um", "bybit-linear", "bybit-inverse"],
          "description": "Download public data: binance-um (Binance USDT-M futures, through 2024-03-30), "
                         "bybit-linear / bybit-inverse (Bybit perpetuals L2 archive, 2023 to yesterday)"}
_MARKET = {
    "venue": _VENUE,
    "pair": {"type": "string", "description": "Symbol for venue, e.g. BTCUSDT"},
    "book": {"type": "string", "description": "Path to your own top-of-book CSV: ts, bid_px, bid_qty, ask_px, ask_qty"},
    "l2": {"type": "array", "items": {"type": "string"},
           "description": "Paths to your own L2 day files (OKX books, Bybit ob*.data) instead of book"},
    "latency_ms": {"type": "number", "description": "Delay each order's arrival at the book (default 0)"},
    "tick": {"type": "number", "description": "Price tick (inferred from the book if omitted)"},
    "offline": {"type": "boolean", "description": "Use cached market data only; never download"},
}

TOOLS = [
    {
        "name": "convergence",
        "title": "Variant-sweep convergence",
        "description": "Has a parameter sweep converged? Reads a CSV of variant backtest results (one row per "
                       "variant: parameters, metrics with _is/_oos suffixes, n_trades) and returns CONVERGED / "
                       "ITERATE / KILL with top-K dispersion, IS/OOS rank stability and the parameter plateau.",
        "inputSchema": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Path to the results CSV"},
            "metric": {"type": "string", "description": "Ranking metric column, e.g. sharpe_oos (auto if omitted)"},
            "top_k": {"type": "integer", "description": "Top variants to assess (default 5)"},
            "dispersion_threshold": {"type": "number", "description": "Max relative dispersion (default 0.05)"},
            "rank_threshold": {"type": "number", "description": "Min IS/OOS Spearman (default 0.6)"},
            "min_samples": {"type": "integer", "description": "Min samples per variant (default 200)"},
            "lower_is_better": {"type": "boolean", "description": "The metric is lower-is-better"},
        }},
    },
    {
        "name": "fill_gap",
        "title": "Fill gap: resting fills vs a queue proxy",
        "description": "Do a backtest's passive (resting limit) fills survive a queue? Re-runs each resting order "
                       "of the order log against public top-of-book and trades under a queue proxy and returns "
                       "CONSISTENT / OVERSTATED with the backtest's fill rate vs the proxy's. Needs market data: "
                       "venue + pair, or book (or l2) + trades.",
        "inputSchema": {"type": "object", "required": ["orders"], "properties": {
            "orders": {"type": "string", "description": "Path to the order log CSV: ts, side, price, size, and the "
                                                        "backtest's filled flag (optional end_ts, pnl)"},
            **_MARKET,
            "trades": {"type": "string", "description": "Path to your own trades CSV, with book or l2"},
            "max_rest": {"type": "number", "description": "Seconds an order rests without an end column (default 60)"},
            "tolerance": {"type": "number", "description": "Share of claimed fills the proxy may miss (default 0.10)"},
        }},
    },
    {
        "name": "cost_gap",
        "title": "Cost gap: taker orders on the real book",
        "description": "What would a backtest's market orders (and limits that cross) really have paid? Re-prices "
                       "each taker order against the book at arrival, with latency, and returns the cost in bps "
                       "vs the price the backtest booked: CONSISTENT / OVERSTATED (with a pnl column) or REPORTED. "
                       "Needs market data: venue + pair, or book (or l2).",
        "inputSchema": {"type": "object", "required": ["orders"], "properties": {
            "orders": {"type": "string", "description": "Path to the order log CSV: ts, side, size, and optionally "
                                                        "type, price, fill_price, pnl"},
            **_MARKET,
            "tolerance": {"type": "number", "description": "Share of P&L the cost may remove (default 0.25)"},
        }},
    },
]
_POSITIONAL = {"convergence": "path", "fill_gap": "orders", "cost_gap": "orders"}


def _argv(tool, args):
    """Tool arguments → the CLI's argv, so the CLI's own parser validates them and fills defaults."""
    schema = next(t for t in TOOLS if t["name"] == tool)["inputSchema"]["properties"]
    unknown = set(args) - set(schema)
    if unknown:
        raise ValueError(f"unknown argument(s) for {tool}: {', '.join(sorted(unknown))}")
    pos = _POSITIONAL[tool]
    if pos not in args:
        raise ValueError(f"{tool} needs {pos!r}")
    argv = [tool.replace("_", "-"), str(args[pos])]
    for k, v in args.items():
        if k == pos or v is None or v is False:
            continue
        flag = "--" + k.replace("_", "-")
        if v is True:
            argv.append(flag)
        elif isinstance(v, list):
            argv += [flag, *map(str, v)]
        else:
            argv += [flag, str(v)]
    return argv


def call(tool, args):
    """Run one check → (markdown, json document). Raises SystemExit("error: ...") on bad input."""
    from backtest2live import cli
    ns = cli.build_parser().parse_args(_argv(tool, args))
    log = lambda m: print(m, file=sys.stderr)  # noqa: E731
    with contextlib.redirect_stdout(sys.stderr):  # stdout is the protocol channel
        if tool == "convergence":
            from backtest2live import convergence
            ctx = convergence.analyze(ns.input, ns)
            return convergence.build_report(ctx), convergence.to_json(ctx)
        try:
            import numpy  # noqa: F401
            import pandas  # noqa: F401
        except ImportError:
            raise SystemExit(f"error: {tool} needs numpy and pandas: pip install 'backtest2live[data]'")
        if tool == "fill_gap":
            from backtest2live.fillgap import engine, report
        else:
            from backtest2live.costgap import engine, report
        res = engine.evaluate(ns, log=log)
        return report.markdown(res), report.to_json(res)


def _finite(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if isinstance(v, dict):
        return {k: _finite(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_finite(x) for x in v]
    return v


def _result(tool, args):
    try:
        md, doc = call(tool, args)
    except ValueError as e:
        return {"content": [{"type": "text", "text": f"error: {e}"}], "isError": True}
    except SystemExit as e:
        # argparse prints its usage error to stderr and exits with an int; evaluators exit with "error: ..."
        msg = e.code if isinstance(e.code, str) else "error: invalid arguments (see the server's stderr)"
        return {"content": [{"type": "text", "text": msg}], "isError": True}
    except Exception as e:  # as in cli.main: a crash in one call must not take the server down
        print(f"{tool}: {type(e).__name__}: {e}", file=sys.stderr)
        return {"content": [{"type": "text", "text": f"error: {type(e).__name__}: {e}"}], "isError": True}
    doc = _finite(json.loads(json.dumps(doc, default=str)))  # numpy scalars → plain JSON, NaN/inf → null
    return {"content": [{"type": "text", "text": md},
                        {"type": "text", "text": json.dumps(doc, indent=2, ensure_ascii=False)}],
            "structuredContent": doc, "isError": False}


def handle(msg):
    """One JSON-RPC message → the response dict, or None for a notification."""
    mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if mid is None:
        return None  # notifications (initialized, cancelled, ...) need no answer
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                  "capabilities": {"tools": {}},
                  "serverInfo": {"name": "backtest2live", "version": __version__},
                  "instructions": "Pre-live checks on a user's own backtest files. Paths are on this machine. "
                                  "Relay each check's verdict and its stated limits."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        if name not in _POSITIONAL:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name!r}"}}
        result = _result(name, params.get("arguments") or {})
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            resp = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            resp = handle(msg) if isinstance(msg, dict) else \
                {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "invalid request"}}
        if resp is not None:
            stdout.write(json.dumps(resp, ensure_ascii=False, allow_nan=False) + "\n")
            stdout.flush()


def add_arguments(p):
    pass


def run(args):
    serve()
    return 0
