"""Markdown and JSON output for fill-gap."""

from datetime import datetime, timezone

from backtest2live.fillgap import CTA_URL, SCHEMA


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _secs(x):
    if x is None:
        return "n/a"
    return f"{x * 1000:.0f} ms" if x < 1 else f"{x:.1f} s"


def _day(us):
    return datetime.fromtimestamp(us / 1e6, tz=timezone.utc).strftime("%Y-%m-%d")


def _summary(res):
    r, c = res["rates"], res["counts"]
    who = "Your backtest fills" if res["has_claim"] else "Filling at touch gives"
    line = (f"{who} {_pct(r['baseline'])} of its resting orders; the queue proxy fills "
            f"{_pct(r['proxy'])}.")
    if c["baseline_fills"]:
        line += (f" {_pct(r['missed_share_of_baseline'])} of the counted fills "
                 f"({c['missed']:,} of {c['baseline_fills']:,}) are not granted on this data.")
    return line


def markdown(res):
    c, r = res["counts"], res["rates"]
    w0, w1 = _day(res["window_utc"][0]), _day(res["window_utc"][1])
    window = w0 if w0 == w1 else f"{w0} .. {w1}"
    L = [f"# Fill Gap | {res['run_name']} | {window}", "",
         f"## Verdict: {res['verdict']}", "", f"**{_summary(res)}**", "", "---", "",
         "## Fill rates", "",
         "| Rule | Fills | Fill rate |", "|------|------:|----------:|"]
    if res["has_claim"]:
        L.append(f"| Your backtest (`filled`) | {c['baseline_fills']:,} | {_pct(r['baseline'])} |")
    L.append(f"| Touch rule: any print at or through your price | {c['touch_fills']:,} | {_pct(r['touch'])} |")
    L.append(f"| Queue proxy | {c['proxy_fills']:,} | {_pct(r['proxy'])} |")
    L += ["",
          f"Rates are over {c['passive']:,} resting orders. Skipped: {c['marketable']:,} marketable at arrival "
          f"(taker orders, out of scope) and {c['no_data']:,} outside the market data. "
          f"{c['never_reached']:,} rested behind the touch and the price never came to them.", ""]
    mt = res["median_time_to_fill_s"]
    L.append(f"Median time to fill: {_secs(mt['touch'])} at touch, {_secs(mt['proxy'])} under the proxy.")
    if c["extra"]:
        L.append(f"The proxy grants {c['extra']:,} fills the baseline did not count.")
    if res["pnl"]:
        p = res["pnl"]
        share = f" ({p['missed_fills'] / p['baseline_fills'] * 100:.0f}% of it)" if p["baseline_fills"] else ""
        L.append(f"P&L your backtest booked on fills the proxy does not grant: {p['missed_fills']:,.2f} "
                 f"of {p['baseline_fills']:,.2f}{share}.")
    L += ["", "---", "", "## Where the fills go missing", ""]
    if res["by_volatility"]:
        L += ["By volatility at arrival (trailing 5-minute range of the mid):", "",
              "| Tercile | Range (bps) | Orders | Baseline | Proxy |", "|--------|------------|-------:|------:|------:|"]
        for v in res["by_volatility"]:
            L.append(f"| {v['tercile']} | {v['range_bps'][0]:.1f}–{v['range_bps'][1]:.1f} | {v['orders']:,} | "
                     f"{_pct(v['baseline'])} | {_pct(v['proxy'])} |")
        L.append("")
    if res["by_hour_utc"]:
        L += ["By hour (UTC):", "", "| Hour | Orders | Baseline | Proxy |", "|-----:|-------:|------:|------:|"]
        for h in res["by_hour_utc"]:
            L.append(f"| {h['hour']:02d} | {h['orders']:,} | {_pct(h['baseline'])} | {_pct(h['proxy'])} |")
        L.append("")
    L += ["---", "", "## Inputs", "",
          f"- Market data: {res['source']}; tick {res['tick']:g}",
          f"- Order life: until `{res['order_end']}`" if not res["order_end"].startswith("--") else
          f"- Order life: {res['order_end']} (no end/cancel column in the log)",
          f"- Arrival latency: {res['latency_ms']:g} ms",
          f"- Baseline: {res['baseline']}; verdict OVERSTATED when more than {_pct(res['tolerance'])} "
          f"of baseline fills are not granted",
          "", "---", "", "## What the proxy can and cannot see", "",
          "The touch rule is an upper bound: a resting order fills only if something trades at or through "
          "its price. The queue proxy is an estimate below that bound, built from public top-of-book and trades:",
          "",
          "- It grants fills too easily where it cannot see hidden or iceberg size ahead of you, your own "
          "latency to reach the book (unless `--latency-ms` is set), or your order's effect on the flow.",
          "- It grants fills too hard where displayed size ahead of you cancels rather than trades, and "
          "where it measures your place in the queue from the size shown when the price reached you.",
          "",
          "Which way it nets out depends on the venue, the size and the regime. Your own live fills settle it: "
          f"the calibrated check against live fills is at {CTA_URL}", ""]
    return "\n".join(L)


def to_json(res):
    out = {"schema": SCHEMA}
    out.update({k: v for k, v in res.items()})
    out["window_utc"] = [_iso(t) for t in res["window_utc"]]
    out["calibrated_check"] = CTA_URL
    return out


def _iso(us):
    return datetime.fromtimestamp(us / 1e6, tz=timezone.utc).isoformat()
