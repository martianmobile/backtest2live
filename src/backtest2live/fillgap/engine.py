"""Load inputs, run the simulation day by day, and aggregate the result."""

import os

import numpy as np

from backtest2live.fillgap import data, sim

US_PER_S = 1_000_000
US_PER_H = 3_600 * US_PER_S


def _ticks(px, tick):
    return np.rint(np.asarray(px, np.float64) / tick).astype(np.int64)


OFF_GRID_MAX_SHARE = 0.01  # above this, the tick is wrong; below it, the odd malformed row is snapped


def _check_grid(orders, tick, log):
    """Once per run, over the whole log: a wrong tick puts most prices off the grid, a malformed
    row puts one (the public archive carries the occasional one). Trip on the share, snap the few.
    `tick` is one value, or one per order when the venue changed the tick inside the window."""
    tick = np.asarray(tick, np.float64)
    grid = f"{float(tick.flat[0]):g}" if np.unique(tick).size == 1 else "day's"
    px = orders["px_f"] / tick
    off = np.abs(px - np.rint(px)) > 1e-6
    if not off.any():
        return
    share = off.mean()
    if share > OFF_GRID_MAX_SHARE:
        raise SystemExit(f"error: {share:.1%} of order prices are not on the {grid} tick grid "
                         f"(first: {orders['px_f'][off][0]:g}); pass --tick or fix the log")
    log(f"fill-gap: {int(off.sum())} of {len(px):,} order prices off the {grid} tick grid; "
        f"snapped to the nearest tick")


def _run_chunk(book, trades, orders, idx, tick, latency_us):
    b = {"t": book["t"], "bid_px": _ticks(book["bid_px"], tick), "ask_px": _ticks(book["ask_px"], tick),
         "bid_qty": book["bid_qty"], "ask_qty": book["ask_qty"]}
    tr = {"t": trades["t"], "px": _ticks(trades["px"], tick), "qty": trades["qty"],
          "sell_aggr": trades["sell_aggr"]}
    o = {"t": orders["t"][idx], "t_end": orders["t_end"][idx], "buy": orders["buy"][idx],
         "px": _ticks(orders["px_f"][idx], tick), "size": orders["size"][idx]}  # off-grid rows snap; see _check_grid
    r = sim.simulate(b, tr, o, latency_us)
    r["range_bps"] = sim.trailing_range_bps(
        {"t": book["t"], "bid_px": book["bid_px"], "ask_px": book["ask_px"]}, r["t_arr"]) \
        if len(book["t"]) else np.full(len(idx), np.nan)
    return r


def evaluate(args, log=print):
    own = bool(args.book or getattr(args, "l2", None) or args.trades)
    if bool(args.venue) == own:
        raise SystemExit("error: pass either --venue with --pair, or your own data (--book or --l2, with --trades)")
    if args.venue and not args.pair:
        raise SystemExit("error: --venue needs --pair (e.g. --pair BTCUSDT)")
    if args.book and getattr(args, "l2", None):
        raise SystemExit("error: pass --book or --l2, not both")
    if own and not ((args.book or getattr(args, "l2", None)) and args.trades):
        raise SystemExit("error: --book (or --l2) and --trades go together")

    orders = data.load_orders(args.orders, args.max_rest)
    n = len(orders["t"])
    latency_us = int(round(args.latency_ms * 1000))
    tick = args.tick

    tick_change = None
    fields = ("status", "queue", "t_act", "touch", "t_touch", "proxy", "t_proxy", "t_arr", "range_bps")
    out = {k: None for k in fields}

    def put(idx, r):
        for k in fields:
            if out[k] is None:
                out[k] = np.zeros(n, dtype=r[k].dtype)
                if r[k].dtype.kind in "if":
                    out[k][:] = -1 if r[k].dtype.kind == "i" else np.nan
            out[k][idx] = r[k]

    if not args.venue:
        if args.book:
            book, bname = data.load_user_book(args.book), os.path.basename(args.book)
        else:
            book, bname = data.load_l2_files(args.l2, log=log), ", ".join(os.path.basename(f) for f in args.l2)
        trades = data.load_user_trades(args.trades)
        tick = tick or data.infer_tick(book)
        _check_grid(orders, tick, log)
        put(np.arange(n), _run_chunk(book, trades, orders, np.arange(n), tick, latency_us))
        source = f"your files ({bname}, {os.path.basename(args.trades)})"
    else:
        pair = args.pair.upper()
        t_arr = orders["t"] + latency_us
        day_of = (t_arr // (86_400 * US_PER_S))
        if tick is not None:
            _check_grid(orders, tick, log)
        ticks, day_ticks = np.full(n, np.nan), []
        for d in np.unique(day_of):
            idx = np.flatnonzero(day_of == d)
            days = data.utc_days(int(t_arr[idx].min()), int(orders["t_end"][idx].max()))
            book, trades = data.load_venue(args.venue, pair, days, offline=args.offline, log=log)
            day_tick = args.tick or data.infer_tick(book)  # per day: a venue can change the tick mid-window
            ticks[idx] = day_tick
            day_ticks.append((days[0], day_tick))
            put(idx, _run_chunk(book, trades, orders, idx, day_tick, latency_us))
        if args.tick is None:
            _check_grid(orders, ticks, log)  # whole log, each order against its own day's tick
            tick = min(t for _, t in day_ticks)
            tick_change = data.tick_note(day_ticks)
            if tick_change:
                log(f"fill-gap: the venue changed the tick inside the window ({tick_change}); "
                    f"each day runs on its own tick")
        source = data.venue_source(args.venue, pair)

    return aggregate(orders, out, args, tick, source, tick_change)


def _rate(mask, base):
    k = int(base.sum())
    return float(mask[base].mean()) if k else None


def _median_s(t_fill, t_arr, mask):
    m = mask & (t_fill >= 0)
    return float(np.median((t_fill[m] - t_arr[m]) / US_PER_S)) if m.any() else None


def aggregate(orders, r, args, tick, source, tick_change=None):
    st = r["status"]
    passive = (st == sim.ACTIVE) | (st == sim.NEVER_REACHED)
    claimed = orders["claimed"]
    has_claim = claimed is not None
    base = claimed if has_claim else r["touch"]
    proxy = r["proxy"]

    missed = passive & base & ~proxy
    extra = passive & proxy & ~base
    n_base = int((passive & base).sum())
    missed_share = (int(missed.sum()) / n_base) if n_base else 0.0
    verdict = "OVERSTATED" if missed_share > args.tolerance else "CONSISTENT"

    hours = ((r["t_arr"] // US_PER_H) % 24).astype(int)
    by_hour = []
    for h in range(24):
        m = passive & (hours == h)
        if m.any():
            by_hour.append({"hour": h, "orders": int(m.sum()), "baseline": _rate(base, m),
                            "proxy": _rate(proxy, m)})

    by_vol = []
    rng = r["range_bps"]
    ok = passive & np.isfinite(rng)
    if ok.sum() >= 3:
        cuts = np.quantile(rng[ok], [1 / 3, 2 / 3])
        tercile = np.where(rng <= cuts[0], 0, np.where(rng <= cuts[1], 1, 2))
        lo = float(np.nanmin(rng[ok]))
        edges = [lo, float(cuts[0]), float(cuts[1]), float(np.nanmax(rng[ok]))]
        for q, name in enumerate(("low", "mid", "high")):
            m = ok & (tercile == q)
            by_vol.append({"tercile": name, "range_bps": [edges[q], edges[q + 1]], "orders": int(m.sum()),
                           "baseline": _rate(base, m), "proxy": _rate(proxy, m)})

    pnl = None
    if orders["pnl"] is not None:
        p = np.nan_to_num(orders["pnl"])
        pnl = {"baseline_fills": float(p[passive & base].sum()), "missed_fills": float(p[missed].sum())}

    t = orders["t"]
    return {
        "verdict": verdict,
        "orders_file": args.orders,
        "run_name": args.run_name or os.path.basename(args.orders),
        "source": source,
        "window_utc": [int(t.min()), int(orders["t_end"].max())],
        "tick": tick,
        "tick_change": tick_change,
        "latency_ms": args.latency_ms,
        "order_end": orders["end_source"],
        "tolerance": args.tolerance,
        "baseline": "your backtest's fill flag" if has_claim else "touch rule (no fill flag in the log)",
        "has_claim": has_claim,
        "counts": {
            "orders": int(len(t)),
            "passive": int(passive.sum()),
            "marketable": int((st == sim.MARKETABLE).sum()),
            "no_data": int((st == sim.NO_DATA).sum()),
            "never_reached": int((st == sim.NEVER_REACHED).sum()),
            "baseline_fills": n_base,
            "touch_fills": int((passive & r["touch"]).sum()),
            "proxy_fills": int((passive & proxy).sum()),
            "missed": int(missed.sum()),
            "extra": int(extra.sum()),
        },
        "rates": {
            "baseline": _rate(base, passive),
            "touch": _rate(r["touch"], passive),
            "proxy": _rate(proxy, passive),
            "missed_share_of_baseline": missed_share,
        },
        "median_time_to_fill_s": {
            "touch": _median_s(r["t_touch"], r["t_arr"], passive & r["touch"]),
            "proxy": _median_s(r["t_proxy"], r["t_arr"], passive & proxy),
        },
        "by_hour_utc": by_hour,
        "by_volatility": by_vol,
        "pnl": pnl,
        "missed_ids": orders["id"][missed][:1000].tolist(),
    }
