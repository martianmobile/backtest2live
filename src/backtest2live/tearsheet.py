"""backtest2live · tearsheet — one shareable performance sheet, labelled with where its numbers came from.

The metrics are commodity; the label is the point. Every sheet states its
provenance in the header, and the label cannot be left out:

    backtest       simulated fills; the assumptions are listed on the sheet
    self-reported  the user's own live trading, read from their export; account and period chosen by them

A third level, recorded by Martian Mobile on a Validate pilot, is produced by
that infrastructure and carries a verification link; this local tool never
writes it.

Input is an equity curve (ts, equity) or an order log with a per-order `pnl`
column plus `--capital`. Output: Markdown on stdout, and with `--out PREFIX`
an HTML page (print it to PDF from the browser) and the JSON behind it.
Nothing is sent anywhere. Pure standard library.
"""

import csv
import hashlib
import html
import json
import math
import re
import statistics
from datetime import datetime, timezone

from backtest2live import __version__

SCHEMA = "backtest2live.tearsheet/1"
PROVENANCE = {
    "backtest": "Backtest — simulated fills, assumptions listed below",
    "self-reported": "Live, self-reported — read from {venue} trade history; account and period chosen by the user",
}
FOOTER = "Generated with Backtest2Live (https://github.com/martianmobile/backtest2live)"

EQUITY_NAMES = ("equity", "value", "nav", "balance", "portfolio_value", "total_value")
TIME_NAMES = ("ts", "timestamp", "time", "datetime", "date", "created_at")
PNL_NAMES = ("pnl", "pnl_usd", "realized_pnl", "profit", "profit_abs")
FEE_NAMES = ("fee", "fees", "commission", "trade_fee_in_quote")
SIZE_NAMES = ("fill_qty", "size", "qty", "quantity", "amount")
PRICE_NAMES = ("fill_price", "price", "avg_price", "px")
DAY_MS = 86_400_000
MIN_DAYS_ANNUAL = 30  # below this, annualized figures are noise and are not printed


# --- input ------------------------------------------------------------------------------

_FRAC = re.compile(r"(\.\d{6})\d+")


def _ms(v):
    s = str(v).strip()
    try:
        x = float(s)
    except ValueError:
        try:
            d = datetime.fromisoformat(_FRAC.sub(r"\1", s.replace("Z", "+00:00")))
        except ValueError:
            raise SystemExit(f"error: unparseable timestamp {s!r}")
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return int(round(d.timestamp() * 1000))
    for lim, f in ((1e17, 1e-6), (1e14, 1e-3), (1e11, 1.0)):
        if abs(x) > lim:
            return int(round(x * f))
    return int(round(x * 1000))


def _num(v):
    try:
        x = float(str(v).strip())
    except ValueError:
        return None
    return x if math.isfinite(x) else None


def _col(headers, names):
    low = {h.strip().lower(): h for h in headers}
    return next((low[n] for n in names if n in low), None)


def load(path, capital):
    """→ dict with points [(ms, equity)] and, for an order log, the per-order arrays."""
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            headers = reader.fieldnames or []
            rows = list(reader)
    except FileNotFoundError:
        raise SystemExit(f"error: file not found: {path!r}")
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise SystemExit(f"error: could not read {path!r}: {e}")
    if not rows:
        raise SystemExit(f"error: no data rows in {path!r}")
    tcol = _col(headers, TIME_NAMES)
    if not tcol:
        raise SystemExit(f"error: no time column (one of {', '.join(TIME_NAMES)}) in {path!r}")
    ecol, pcol = _col(headers, EQUITY_NAMES), _col(headers, PNL_NAMES)

    if ecol:
        pts = sorted((_ms(r[tcol]), _num(r[ecol])) for r in rows if _num(r[ecol]) is not None)
        if len(pts) < 2:
            raise SystemExit("error: the equity curve needs at least two numeric points")
        if pts[0][1] <= 0:
            raise SystemExit("error: the equity curve must start above zero")
        return {"kind": "equity curve", "points": pts, "orders": None, "capital": pts[0][1]}

    if not pcol:
        raise SystemExit(f"error: {path!r} has neither an equity column ({', '.join(EQUITY_NAMES)}) "
                         f"nor a pnl column ({', '.join(PNL_NAMES)})")
    if not capital or capital <= 0:
        raise SystemExit("error: an order log needs --capital (the starting equity) to turn P&L into returns")
    fcol, scol, xcol = _col(headers, FEE_NAMES), _col(headers, SIZE_NAMES), _col(headers, PRICE_NAMES)
    side = _col(headers, ("side", "direction"))
    orders = []
    for r in rows:
        orders.append({
            "t": _ms(r[tcol]), "pnl": _num(r[pcol]) or 0.0,
            "fee": abs(_num(r[fcol]) or 0.0) if fcol else 0.0,
            "notional": abs((_num(r[scol]) or 0.0) * (_num(r[xcol]) or 0.0)) if scol and xcol else None,
            "signed": ((_num(r[scol]) or 0.0) * (1 if str(r[side]).strip().lower() in ("buy", "b", "long") else -1))
            if scol and side else None,
        })
    orders.sort(key=lambda o: o["t"])
    eq, pts = capital, [(orders[0]["t"], capital)]
    for o in orders:
        eq += o["pnl"]
        pts.append((o["t"], eq))
    return {"kind": "order log (equity = capital + realized P&L; open positions are not marked)",
            "points": pts, "orders": orders, "capital": capital, "fee_column": fcol}


# --- metrics --------------------------------------------------------------------------------

def daily(points):
    """Last equity of each UTC day, with flat days filled forward."""
    last = {}
    for t, e in points:
        last[t // DAY_MS] = e
    d0, d1 = min(last), max(last)
    out, e = [], points[0][1]
    for d in range(d0, d1 + 1):
        e = last.get(d, e)
        out.append((d, e))
    return out


def _dd(curve):
    peak, peak_i, worst, worst_dur = curve[0][1], 0, 0.0, 0
    for i, (_, e) in enumerate(curve):
        if e >= peak:
            peak, peak_i = e, i
        dd = e / peak - 1 if peak > 0 else 0.0
        if dd < worst:
            worst = dd
        worst_dur = max(worst_dur, i - peak_i)
    return worst, worst_dur


def metrics(src, periods_per_year):
    curve = daily(src["points"])
    start_eq = src["capital"]
    # an equity curve's first row is its starting point (a day-0 close); an order log starts from
    # --capital before the first order, so day 0 carries that day's P&L
    eqs = [e for _, e in curve] if src["orders"] is None else [start_eq] + [e for _, e in curve]
    rets = [eqs[i] / eqs[i - 1] - 1 for i in range(1, len(eqs)) if eqs[i - 1] > 0]
    n = len(rets)
    total = eqs[-1] / start_eq - 1
    years = n / periods_per_year
    annual = n >= MIN_DAYS_ANNUAL
    cagr = (1 + total) ** (1 / years) - 1 if annual and total > -1 else None
    vol = statistics.stdev(rets) * math.sqrt(periods_per_year) if annual else None
    mean = statistics.mean(rets) if rets else 0.0
    sharpe = (mean / statistics.stdev(rets) * math.sqrt(periods_per_year)) if annual and statistics.stdev(rets) > 0 \
        else None
    mdd, mdd_days = _dd([(None, e) for e in eqs])
    mar = cagr / abs(mdd) if cagr is not None and mdd < 0 else None

    def worst(k):
        if n < k:
            return None
        w = None
        for i in range(k, len(eqs)):
            r = eqs[i] / eqs[i - k] - 1
            if w is None or r < w[0]:
                w = (r, curve[i - (len(eqs) - len(curve))][0])
        return {"return": w[0], "ending": _day(w[1] * DAY_MS)}

    monthly = {}
    prev = start_eq
    for d, e in curve:
        key = _day(d * DAY_MS)[:7]
        m = monthly.setdefault(key, [prev, e])
        m[1] = e
        prev = e
    out = {
        "days": n,
        "start": _day(curve[0][0] * DAY_MS), "end": _day(curve[-1][0] * DAY_MS),
        "start_equity": start_eq, "end_equity": eqs[-1],
        "total_return": total, "cagr": cagr, "volatility": vol, "sharpe": sharpe,
        "max_drawdown": mdd, "max_drawdown_days": mdd_days, "mar": mar,
        "worst": {"day": worst(1), "week": worst(7), "month": worst(30)},
        "monthly": {k: v[1] / v[0] - 1 for k, v in monthly.items() if v[0] > 0},
        "periods_per_year": periods_per_year,
    }
    o = src["orders"]
    if o:
        wins = sum(x["pnl"] for x in o if x["pnl"] > 0)
        losses = -sum(x["pnl"] for x in o if x["pnl"] < 0)
        out["orders"] = len(o)
        out["profit_factor"] = wins / losses if losses > 0 else None
        notional = [x["notional"] for x in o if x["notional"] is not None]
        if notional:
            avg_eq = statistics.mean(eqs)
            out["turnover_per_year"] = sum(notional) / avg_eq / years if annual and avg_eq > 0 else None
        out["fees"] = sum(x["fee"] for x in o) if src.get("fee_column") else None
        if out["fees"] is not None and notional and sum(notional) > 0:
            out["fee_rate_bps"] = out["fees"] / sum(notional) * 1e4
        if all(x["signed"] is not None for x in o):
            # time with a net position open, between the first and last order
            pos, held, prev_t = 0.0, 0, o[0]["t"]
            for x in o:
                if abs(pos) > 1e-12:
                    held += x["t"] - prev_t
                pos += x["signed"]
                prev_t = x["t"]
            span = o[-1]["t"] - o[0]["t"]
            out["exposure"] = held / span if span > 0 else None
    return out, curve


def _day(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


# --- output ----------------------------------------------------------------------------------

def _pct(x, nd=1):
    return "n/a" if x is None else f"{x * 100:.{nd}f}%"


def _f(x, nd=2):
    return "n/a" if x is None else f"{x:,.{nd}f}"


def _rows(m):
    w = m["worst"]
    rows = [
        ("Period", f"{m['start']} to {m['end']} ({m['days']} daily returns"
                   + (f"; annualized figures need {MIN_DAYS_ANNUAL})" if m["days"] < MIN_DAYS_ANNUAL else ")")),
        ("Total return", _pct(m["total_return"])),
        ("CAGR", _pct(m["cagr"])),
        ("Volatility (annualized)", _pct(m["volatility"])),
        ("Sharpe (annualized, rf = 0)", _f(m["sharpe"])),
        ("Max drawdown (daily closes)", f"{_pct(m['max_drawdown'])} (longest under water: {m['max_drawdown_days']} d)"),
        ("MAR (CAGR / max drawdown)", _f(m["mar"])),
        ("Worst day / 7 days / 30 days", " / ".join(_pct(w[k]["return"]) if w[k] else "n/a"
                                                  for k in ("day", "week", "month"))),
    ]
    if "orders" in m:
        rows.append(("Orders", f"{m['orders']:,}"))
        rows.append(("Profit factor", _f(m["profit_factor"])))
        if "turnover_per_year" in m:
            rows.append(("Turnover (notional / average equity, per year)", _f(m["turnover_per_year"], 1) + "×"))
        if m.get("fees") is not None:
            rate = f" ({_f(m.get('fee_rate_bps'))} bps of notional)" if m.get("fee_rate_bps") is not None else ""
            rows.append(("Fees paid", _f(m["fees"]) + rate))
        if m.get("exposure") is not None:
            rows.append(("Time in market", _pct(m["exposure"], 0)))
    return rows


def build(src, args):
    m, curve = metrics(src, args.periods_per_year)
    if args.provenance == "self-reported" and not args.venue:
        raise SystemExit("error: a self-reported sheet names the venue its history came from: pass --venue")
    label = PROVENANCE[args.provenance].format(venue=args.venue or "")
    checks = []
    for p in args.check or []:
        try:
            with open(p, encoding="utf-8") as fh:
                doc = json.load(fh)
        except (OSError, ValueError) as e:
            raise SystemExit(f"error: could not read the check result {p!r}: {e}")
        checks.append({"schema": doc.get("schema"), "verdict": doc.get("verdict"), "file": p})
    doc = {
        "schema": SCHEMA,
        "generator": f"backtest2live {__version__}",
        "title": args.title or src["path"],
        "provenance": {"level": args.provenance, "label": label},
        "source": src["kind"],
        "assumptions": list(args.assumption or []),
        "checks": checks,
        "metrics": m,
        "equity_daily": [[_day(d * DAY_MS), e] for d, e in curve],
    }
    body = json.dumps(doc, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    doc["sha256"] = hashlib.sha256(body).hexdigest()  # of the sheet without this field
    return doc


def markdown(doc):
    m = doc["metrics"]
    L = [f"# Tear sheet | {doc['title']}", "", f"**{doc['provenance']['label']}**", "",
         "| | |", "|---|---|"]
    L += [f"| {k} | {v} |" for k, v in _rows(m)]
    L += ["", "## Monthly returns", "", "| Month | Return |", "|---|---:|"]
    L += [f"| {k} | {_pct(v)} |" for k, v in m["monthly"].items()]
    L += ["", "## Source and assumptions", "", f"- Source: {doc['source']}"]
    L += [f"- {a}" for a in doc["assumptions"]]
    if doc["provenance"]["level"] == "backtest" and not doc["assumptions"]:
        L.append("- No assumptions were given (`--assumption`): the fill model behind these numbers is not stated.")
    for c in doc["checks"]:
        L.append(f"- Check `{c['schema']}`: **{c['verdict']}** ({c['file']})")
    L += ["", "---", "", f"{FOOTER} · sheet sha256 `{doc['sha256'][:16]}…`", ""]
    return "\n".join(L)


def _svg(curve, w=720, h=180):
    vals = [e for _, e in curve]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    n = max(len(vals) - 1, 1)
    pts = " ".join(f"{i / n * w:.1f},{h - (v - lo) / span * (h - 10) - 5:.1f}" for i, v in enumerate(vals))
    return (f'<svg viewBox="0 0 {w} {h}" width="100%" role="img" aria-label="Equity curve">'
            f'<polyline fill="none" stroke="currentColor" stroke-width="1.5" points="{pts}"/></svg>')


def to_html(doc):
    m = doc["metrics"]
    e = html.escape
    rows = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in _rows(m))
    months = "".join(f"<tr><th>{e(k)}</th><td>{e(_pct(v))}</td></tr>" for k, v in m["monthly"].items())
    notes = "".join(f"<li>{e(a)}</li>" for a in [f"Source: {doc['source']}"] + doc["assumptions"])
    notes += "".join(f"<li>Check {e(str(c['schema']))}: <b>{e(str(c['verdict']))}</b></li>" for c in doc["checks"])
    curve = [(d, v) for d, v in doc["equity_daily"]]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Tear sheet</title>
<style>
:root{{--fg:#1b1f24;--muted:#5b6470;--line:#d8dde3;--band:#f3f5f7;--bg:#fff}}
@media (prefers-color-scheme:dark){{:root{{--fg:#e6e9ed;--muted:#9aa4af;--line:#38404a;--band:#20262d;--bg:#14181c}}}}
body{{font:14px/1.5 system-ui,sans-serif;color:var(--fg);background:var(--bg);max-width:760px;margin:24px auto;padding:0 16px}}
h1{{font-size:20px;margin:0 0 8px}} h2{{font-size:15px;margin:24px 0 8px}}
.label{{background:var(--band);border-left:4px solid var(--fg);padding:8px 12px;font-weight:600}}
table{{border-collapse:collapse;width:100%}} th,td{{border-bottom:1px solid var(--line);padding:4px 8px;text-align:left}}
td{{text-align:right;font-variant-numeric:tabular-nums}} .muted{{color:var(--muted);font-size:12px}}
@media print{{body{{margin:0}}}}
</style></head><body>
<h1>{e(doc['title'])}</h1>
<div class="label">{e(doc['provenance']['label'])}</div>
<h2>Equity</h2>{_svg(curve)}
<h2>Performance</h2><table>{rows}</table>
<h2>Monthly returns</h2><table>{months}</table>
<h2>Source and assumptions</h2><ul>{notes}</ul>
<p class="muted">{e(FOOTER)} · sheet sha256 {e(doc['sha256'])}</p>
</body></html>
"""


def add_arguments(p):
    p.add_argument("input", help="Equity curve CSV (ts, equity) or an order log with a pnl column (needs --capital)")
    p.add_argument("--provenance", required=True, choices=sorted(PROVENANCE),
                   help="Where the numbers came from; printed in the header. backtest = simulated fills; "
                        "self-reported = your own live trading history")
    p.add_argument("--venue", help="For self-reported: the venue the trade history was exported from")
    p.add_argument("--capital", type=float, help="Starting equity, for an order-log input")
    p.add_argument("--assumption", action="append",
                   help="A backtest assumption to list on the sheet (repeatable), e.g. 'fills at touch, 2 bps taker fee'")
    p.add_argument("--check", action="append", metavar="JSON",
                   help="A bt2live --json result (fill-gap, cost-gap, convergence) to cite on the sheet (repeatable)")
    p.add_argument("--periods-per-year", type=float, default=365.0,
                   help="Days per year for annualizing (default 365, crypto trades every day; 252 for equities)")
    p.add_argument("--title", help="Sheet title (default: the input file name)")
    p.add_argument("--out", metavar="PREFIX", help="Also write PREFIX.html and PREFIX.json")
    p.add_argument("--json", action="store_true", help="Print the sheet's JSON instead of Markdown")


def run(args):
    src = load(args.input, args.capital)
    src["path"] = args.input.replace("\\", "/").split("/")[-1]
    doc = build(src, args)
    if args.out:
        with open(args.out + ".json", "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
        with open(args.out + ".html", "w", encoding="utf-8") as fh:
            fh.write(to_html(doc))
    print(json.dumps(doc, indent=2, ensure_ascii=False) if args.json else markdown(doc))
    return 0
