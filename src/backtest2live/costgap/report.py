"""Markdown and JSON output for cost-gap."""

from datetime import datetime, timezone

from backtest2live.costgap import CTA_URL, SCHEMA


def _bps(x):
    return "n/a" if x is None else f"{x:+.2f} bps"


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.0f}%"


def _day(us):
    return datetime.fromtimestamp(us / 1e6, tz=timezone.utc).strftime("%Y-%m-%d")


def _summary(res):
    c, s = res["counts"], res["cost_bps"]
    if not s:
        return "No taker orders to price: every order in the log rests behind the touch (run `bt2live fill-gap`)."
    line = (f"Your backtest's {c['taker']:,} taker orders would have paid {_bps(s['mean'])} on average "
            f"({_bps(s['median'])} median) against the price it booked.")
    d = res["drag"]
    if d["share_of_pnl"] is not None:
        line += f" That is {_pct(d['share_of_pnl'])} of the P&L the backtest reports."
    return line


def markdown(res):
    c, s, d = res["counts"], res["cost_bps"], res["drag"]
    w0, w1 = _day(res["window_utc"][0]), _day(res["window_utc"][1])
    window = w0 if w0 == w1 else f"{w0} .. {w1}"
    L = [f"# Cost Gap | {res['run_name']} | {window}", "",
         f"## Verdict: {res['verdict']}", "", f"**{_summary(res)}**", "", "---", "",
         "## Execution cost", ""]
    if s:
        L += ["| | Cost vs backtest |", "|---|---:|",
              f"| Mean | {_bps(s['mean'])} |", f"| Median | {_bps(s['median'])} |", f"| 90th percentile | {_bps(s['p90'])} |",
              f"| Total, quote currency | {d['quote']:,.2f} |"]
        if d["pnl"] is not None:
            L.append(f"| Backtest P&L | {d['pnl']:,.2f} |")
        L.append("")
    L += [f"Priced: {c['taker']:,} taker orders (market, or limit crossing the touch). "
          f"{c['resting']:,} limit orders rest behind the touch and are not priced here: that is `bt2live fill-gap`'s job. "
          f"{c['no_data']:,} fall outside the market data.",
          f"{c['beyond_touch']:,} orders were larger than the displayed size at the touch; "
          f"{c['partially_unfilled']:,} limit orders could not fill in full within their limit.", ""]
    L += ["Cost by arrival latency (mean over the same orders):", "",
          "| Latency | Orders | Mean | Median |", "|---:|---:|---:|---:|"]
    for row in res["latency_sweep"]:
        st = row["cost_bps"]
        L.append(f"| {row['latency_ms']:g} ms | {st['n'] if st else 0:,} | {_bps(st['mean'] if st else None)} | "
                 f"{_bps(st['median'] if st else None)} |")
    L += ["", "---", "", "## Where the cost sits", ""]
    if res["by_volatility"]:
        L += ["By volatility at arrival (trailing 5-minute range of the mid):", "",
              "| Tercile | Range (bps) | Orders | Mean | Median |", "|---|---|---:|---:|---:|"]
        for v in res["by_volatility"]:
            st = v["cost_bps"]
            L.append(f"| {v['tercile']} | {v['range_bps'][0]:.1f}–{v['range_bps'][1]:.1f} | {v['orders']:,} | "
                     f"{_bps(st['mean'] if st else None)} | {_bps(st['median'] if st else None)} |")
        L.append("")
    if res["by_hour_utc"]:
        L += ["By hour (UTC):", "", "| Hour | Orders | Mean | Median |", "|---:|---:|---:|---:|"]
        for h in res["by_hour_utc"]:
            st = h["cost_bps"]
            L.append(f"| {h['hour']:02d} | {h['orders']:,} | {_bps(st['mean'] if st else None)} | "
                     f"{_bps(st['median'] if st else None)} |")
        L.append("")
    L += ["---", "", "## Inputs", "",
          f"- Market data: {res['source']}; tick {res['tick']:g}",
          f"- Baseline (what the backtest booked): {res['baseline']}",
          f"- Arrival latency: {res['latency_ms']:g} ms",
          f"- Verdict OVERSTATED when the cost removes more than {_pct(res['tolerance'])} of the backtest's P&L "
          f"(needs a `pnl` column; without one the verdict is REPORTED)",
          "", "---", "", "## What the book can and cannot see", "",
          "Top-of-book shows the price and displayed size at the touch, and nothing behind it. Size beyond the touch "
          "is priced one tick worse per displayed-size slice, the least impact consistent with the feed. Real depth "
          "is usually thinner than that behind the first level and hidden size can be thicker, so this is a floor on "
          "impact, not a measurement. Latency is the other half: the table above shows how fast the cost grows when "
          "your order arrives after the book you saw.",
          "",
          f"Your own live fills settle it: the calibrated check against live execution is at {CTA_URL}", ""]
    return "\n".join(L)


def to_json(res):
    out = {"schema": SCHEMA}
    out.update(res)
    out["window_utc"] = [_iso(t) for t in res["window_utc"]]
    out["calibrated_check"] = CTA_URL
    return out


def _iso(us):
    return datetime.fromtimestamp(us / 1e6, tz=timezone.utc).isoformat()
