"""backtest2live · preflight — every available check in one report.

Four sections, the shape of a live desk's review gate: out-of-sample,
execution realism, capacity, risk. Each is marked pass / warn / fail /
not run, with what a local check on public data cannot see. The checks are
the existing evaluators run in-process; preflight adds no math of its own.

    bt2live preflight --results sweep.csv --orders orders.csv --venue bybit-linear --pair BTCUSDT

Exit code: 0 every check that ran passed, 1 a warning, 2 a failure, 3 error.
"""

import contextlib
import io
import json
import os
import sys

SCHEMA = "backtest2live.preflight/1"
RANK = {"pass": 0, "not run": 0, "warn": 1, "fail": 2}
STATUS = {
    "convergence": {"CONVERGED": "pass", "ITERATE": "warn", "KILL": "fail"},
    "fill-gap": {"CONSISTENT": "pass", "OVERSTATED": "fail"},
    "cost-gap": {"CONSISTENT": "pass", "REPORTED": "warn", "OVERSTATED": "fail"},
}
LIMITS = {
    "out_of_sample": "Reads the sweep's own in-sample and out-of-sample numbers. It cannot tell whether the "
                     "out-of-sample window was touched while designing the strategy, and a sweep over many "
                     "correlated variants can still pick a lucky one.",
    "execution": "Public top-of-book (and L2 where the source has it) and trade prints. Hidden size, cancels ahead "
                 "of you, your own latency and your own impact are not visible; only your live fills settle it.",
    "capacity": "No capacity check exists yet. cost-gap's beyond-the-touch pricing is the nearest proxy: rerun it "
                "with your orders scaled up.",
    "risk": "No risk check exists yet (drawdown, exposure and order-rate limits).",
}


def _quiet(fn, *a, **kw):
    """Run an evaluator with its stdout captured: preflight prints one report."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        return fn(*a, **kw)


def _sub(argv):
    from backtest2live import cli
    return cli.build_parser().parse_args(argv)


def _fail_reason(e):
    msg = e.code if isinstance(e.code, str) else f"exit {e.code}"
    return msg[len("error: "):] if msg.startswith("error: ") else msg


def _convergence(args, log):
    if not args.results:
        return {"check": "convergence", "status": "not run", "reason": "no --results sweep given"}
    from backtest2live import convergence
    argv = ["convergence", args.results, "--json"] + (["--metric", args.metric] if args.metric else [])
    try:
        ctx = _quiet(convergence.analyze, args.results, _sub(argv))
    except SystemExit as e:
        return {"check": "convergence", "status": "not run", "reason": _fail_reason(e)}
    doc = convergence.to_json(ctx)
    return {"check": "convergence", "status": STATUS["convergence"][doc["verdict"]], "verdict": doc["verdict"],
            "summary": convergence._summary_line(ctx), "result": doc}


def _market_argv(args, with_trades):
    out = []
    for flag, v in (("--venue", args.venue), ("--pair", args.pair), ("--book", args.book),
                    ("--trades", args.trades if with_trades else None), ("--tick", args.tick)):
        if v is not None:
            out += [flag, str(v)]
    if args.l2:
        out += ["--l2", *args.l2]
    if args.offline:
        out.append("--offline")
    out += ["--latency-ms", str(args.latency_ms)]
    return out


RESTING_COLUMNS = ("filled", "is_filled", "fill", "backtest_filled", "fill_qty", "filled_qty", "executed_qty",
                   "end_ts", "cancel_ts", "expire_ts", "cancel_time", "end_time", "expiry")


def _untyped_shape(path):
    """A log without a `type` column holds one kind of order: resting limits (it has a fill flag or an end
    of life: fill-gap's shape) or a bar backtest's fills (cost-gap's). None when it has a type column."""
    import csv
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            header = [h.strip().lower() for h in next(csv.reader(fh), [])]
    except OSError:
        return None  # the evaluator reports the read error
    if {"type", "order_type", "ord_type"} & set(header):
        return None
    return "fill-gap" if set(RESTING_COLUMNS) & set(header) else "cost-gap"


def _evaluator(name, args, log):
    if not args.orders:
        return {"check": name, "status": "not run", "reason": "no --orders log given"}
    shape = _untyped_shape(args.orders)
    if shape and shape != name:
        kind = "resting orders (fill flag or end time)" if shape == "fill-gap" else "fills with no fill flag"
        return {"check": name, "status": "not run",
                "reason": f"the log has no `type` column and reads as {kind}; add `type` (market/limit) to run both"}
    try:
        import numpy  # noqa: F401
        import pandas  # noqa: F401
    except ImportError:
        return {"check": name, "status": "not run", "reason": "needs numpy and pandas: pip install 'backtest2live[data]'"}
    if name == "fill-gap":
        from backtest2live.fillgap import engine, report
        sub = _sub(["fill-gap", args.orders, *_market_argv(args, True)])
    else:
        from backtest2live.costgap import engine, report
        sub = _sub(["cost-gap", args.orders, *_market_argv(args, False)])
    try:
        res = _quiet(engine.evaluate, sub, log=log)
    except SystemExit as e:
        return {"check": name, "status": "not run", "reason": _fail_reason(e)}
    if name == "fill-gap" and not res["counts"]["passive"]:
        return {"check": name, "status": "not run", "reason": "no resting orders in the log"}
    if name == "cost-gap" and not res["counts"]["taker"]:
        return {"check": name, "status": "not run", "reason": "no taker orders in the log"}
    doc = report.to_json(res)
    return {"check": name, "status": STATUS[name][res["verdict"]], "verdict": res["verdict"],
            "summary": report._summary(res), "result": doc}


def _worst(checks):
    ran = [c["status"] for c in checks if c["status"] != "not run"]
    return max(ran, key=RANK.get) if ran else "not run"


def evaluate(args, log=print):
    if not (args.results or args.orders):
        raise SystemExit("error: pass --results (a sweep), --orders (an order log with market data), or both")
    conv = _convergence(args, log)
    fg, cg = _evaluator("fill-gap", args, log), _evaluator("cost-gap", args, log)
    sections = [
        {"section": "out_of_sample", "title": "Out-of-sample", "question": "Did I overfit the search?",
         "checks": [conv]},
        {"section": "execution", "title": "Execution realism", "question": "Would I actually have been filled, "
                                                                          "and at what price?",
         "checks": [fg, cg]},
        {"section": "capacity", "title": "Capacity", "question": "How much size before the edge is gone?",
         "checks": [{"check": "capacity", "status": "not run", "reason": "not built yet"}]},
        {"section": "risk", "title": "Risk", "question": "Does it stay inside limits?",
         "checks": [{"check": "risk", "status": "not run", "reason": "not built yet"}]},
    ]
    for s in sections:
        s["status"] = _worst(s["checks"])
        s["limits"] = LIMITS[s["section"]]
    overall = _worst([{"status": s["status"]} for s in sections])
    return {"schema": SCHEMA, "run_name": args.run_name or os.path.basename(args.orders or args.results),
            "status": overall, "sections": sections}


MARK = {"pass": "PASS", "warn": "WARN", "fail": "FAIL", "not run": "NOT RUN"}


def markdown(doc):
    L = [f"# Preflight | {doc['run_name']}", "", f"## Overall: {MARK[doc['status']]}", "",
         "| Check | Status |", "|---|---|"]
    L += [f"| {s['title']} | {MARK[s['status']]} |" for s in doc["sections"]]
    for s in doc["sections"]:
        L += ["", "---", "", f"## {s['title']}: {MARK[s['status']]}", "", f"*{s['question']}*", ""]
        for c in s["checks"]:
            if c["status"] == "not run":
                L.append(f"- `{c['check']}`: not run ({c['reason']})")
            else:
                L.append(f"- `{c['check']}`: **{c['verdict']}** ({MARK[c['status']]}). {c['summary']}")
        L += ["", f"What a local check on public data cannot see: {s['limits']}"]
    L += ["", "---", "", "Each check's full report: run it on its own (`bt2live <check> … `), or read `--json`.", ""]
    return "\n".join(L)


def add_arguments(p):
    p.add_argument("--results", help="Variant sweep CSV (one row per variant): runs convergence")
    p.add_argument("--metric", help="Ranking metric for convergence (auto-detected if omitted)")
    p.add_argument("--orders", help="Order log CSV: runs fill-gap on its resting orders and cost-gap on its takers")
    src = p.add_argument_group("market data for --orders (as for fill-gap / cost-gap)")
    src.add_argument("--venue", choices=("binance-um", "bybit-linear", "bybit-inverse"))
    src.add_argument("--pair")
    src.add_argument("--book")
    src.add_argument("--l2", nargs="+", metavar="FILE")
    src.add_argument("--trades", help="Trades CSV, with --book / --l2 (fill-gap needs it)")
    src.add_argument("--tick", type=float)
    src.add_argument("--latency-ms", type=float, default=0.0)
    src.add_argument("--offline", action="store_true")
    p.add_argument("--run-name", help="Label for the report header")
    p.add_argument("--out", metavar="PREFIX", help="Also write PREFIX.md and PREFIX.json")
    p.add_argument("--json", action="store_true", help="Print the JSON instead of Markdown")


EXIT = {"pass": 0, "not run": 0, "warn": 1, "fail": 2}


def run(args):
    doc = evaluate(args, log=lambda m: print(m, file=sys.stderr))
    md = markdown(doc)
    if args.out:
        with open(args.out + ".md", "w", encoding="utf-8") as fh:
            fh.write(md)
        with open(args.out + ".json", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
    print(json.dumps(doc, indent=2, ensure_ascii=False) if args.json else md)
    return EXIT[doc["status"]]
