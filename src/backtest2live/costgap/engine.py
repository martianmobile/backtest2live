"""Load the taker order log, price it day by day, aggregate."""

import os

import numpy as np
import pandas as pd

from backtest2live.costgap import LATENCY_SWEEP_MS, sim
from backtest2live.fillgap import data
from backtest2live.fillgap.sim import trailing_range_bps

US_PER_S = 1_000_000
US_PER_H = 3_600 * US_PER_S

ALIASES = {
    "t": data.ORDER_ALIASES["t"],
    "side": data.ORDER_ALIASES["side"],
    "size": data.ORDER_ALIASES["size"],
    "type": ("type", "order_type", "ord_type"),
    "px": ("price", "px", "limit_price", "limit"),
    "fill_px": ("fill_price", "fill_px", "avg_price", "avg_fill_price", "executed_price"),
    "pnl": data.ORDER_ALIASES["pnl"],
    "id": data.ORDER_ALIASES["id"],
}
MARKET_WORDS = {"market", "mkt", "m"}
LIMIT_WORDS = {"limit", "lmt", "l"}


def load_orders(path, log=print):
    df = data._read_csv(path, "order log")
    c = data._resolve(df, ALIASES, ("t", "side", "size"), "order log")
    side = df[c["side"]].astype(str).str.strip().str.lower()
    unknown = ~side.isin(data.BUY_WORDS | data.SELL_WORDS)
    if unknown.any():
        raise SystemExit(f"error: side must be buy/sell; got {side[unknown].iloc[0]!r}")

    n = len(df)
    if "px" in c:
        limit = pd.to_numeric(df[c["px"]], errors="coerce").to_numpy(np.float64)  # blank = market
    else:
        limit = np.full(n, np.nan)
    if "type" in c:
        typ = df[c["type"]].astype(str).str.strip().str.lower()
        bad = ~typ.isin(MARKET_WORDS | LIMIT_WORDS)
        if bad.any():
            raise SystemExit(f"error: type must be market/limit; got {typ[bad].iloc[0]!r}")
        is_market = typ.isin(MARKET_WORDS).to_numpy()
        limit = np.where(is_market, np.nan, limit)
        if (~is_market & ~np.isfinite(limit)).any():
            raise SystemExit("error: a limit order needs a price")
    elif "px" in c and "fill_px" not in c:
        # Untyped log with one price column: a bar backtest's export, where the price is the
        # fill it booked, not a limit. Read it as the baseline and treat every order as market.
        log(f"cost-gap: no 'type' column; reading {c['px']!r} as the price the backtest booked "
            f"(all orders priced as market). If it is a limit price, add a 'type' column.")
        c["fill_px"] = c.pop("px")
        limit = np.full(n, np.nan)
    elif not np.isfinite(limit).any() and "px" in c:
        raise SystemExit(f"error: column {c['px']!r} has no numeric prices")

    fill_px = (pd.to_numeric(df[c["fill_px"]], errors="coerce").to_numpy(np.float64)
               if "fill_px" in c else np.full(n, np.nan))
    return {
        "t": data.to_us(df[c["t"]]),
        "buy": side.isin(data.BUY_WORDS).to_numpy(),
        "size": data._numeric(df, c["size"], "size"),
        "limit": limit,
        "fill_px": fill_px,
        "pnl": pd.to_numeric(df[c["pnl"]], errors="coerce").to_numpy(np.float64) if "pnl" in c else None,
        "id": df[c["id"]].astype(str).to_numpy() if "id" in c else np.arange(n).astype(str),
        "baseline_source": f"{c['fill_px']} (mid at arrival where blank)" if "fill_px" in c else "mid at arrival",
        "has_fill_px": "fill_px" in c,
    }


def _price(book, orders, idx, tick, latency_us):
    o = {k: orders[k][idx] for k in ("t", "buy", "size", "limit", "fill_px")}
    r = sim.price_takers(book, o, tick, latency_us)
    r["range_bps"] = trailing_range_bps(book, r["t_arr"]) if len(book["t"]) else np.full(len(idx), np.nan)
    # mean cost at the standard latencies, for the sweep table
    sweep = {}
    for ms in LATENCY_SWEEP_MS:
        rr = sim.price_takers(book, o, tick, int(ms * 1000))
        sweep[ms] = rr["cost_bps"]
    r["sweep"] = sweep
    return r


def evaluate(args, log=print):
    if bool(args.venue) == bool(args.book):
        raise SystemExit("error: pass either --venue with --pair, or --book")
    if args.venue and not args.pair:
        raise SystemExit("error: --venue needs --pair (e.g. --pair BTCUSDT)")

    orders = load_orders(args.orders, log)
    n = len(orders["t"])
    latency_us = int(round(args.latency_ms * 1000))
    tick = args.tick

    fields = ("status", "t_arr", "baseline", "vwap", "filled", "unfilled", "beyond_touch", "cost_bps", "range_bps")
    out = {k: None for k in fields}
    sweep = {ms: np.full(n, np.nan) for ms in LATENCY_SWEEP_MS}

    def put(idx, r):
        for k in fields:
            if out[k] is None:
                out[k] = np.zeros(n, dtype=r[k].dtype)
                if r[k].dtype.kind in "if":
                    out[k][:] = -1 if r[k].dtype.kind == "i" else np.nan
            out[k][idx] = r[k]
        for ms in LATENCY_SWEEP_MS:
            sweep[ms][idx] = r["sweep"][ms]

    if args.book:
        book = data.load_user_book(args.book)
        tick = tick or data.infer_tick(book)
        put(np.arange(n), _price(book, orders, np.arange(n), tick, latency_us))
        source = f"your file ({os.path.basename(args.book)})"
    else:
        pair = args.pair.upper()
        t_arr = orders["t"] + latency_us
        day_of = t_arr // (86_400 * US_PER_S)
        for d in np.unique(day_of):
            idx = np.flatnonzero(day_of == d)
            days = data.utc_days(int(t_arr[idx].min()), int(t_arr[idx].max()) + int(max(LATENCY_SWEEP_MS)) * 1000)
            book, _ = data.load_binance_um(pair, days, offline=args.offline, log=log)
            tick = tick or data.infer_tick(book)
            put(idx, _price(book, orders, idx, tick, latency_us))
        source = f"Binance USDT-M futures public archive, {pair}"

    return aggregate(orders, out, sweep, args, tick, source)


def _stats(x):
    x = x[np.isfinite(x)]
    if not len(x):
        return None
    return {"n": int(len(x)), "mean": float(x.mean()), "median": float(np.median(x)),
            "p90": float(np.quantile(x, 0.9))}


def aggregate(orders, r, sweep, args, tick, source):
    st = r["status"]
    taker = st == sim.TAKER
    cost = r["cost_bps"]
    notional = r["filled"] * r["baseline"]
    drag = np.where(taker & np.isfinite(cost), cost / 1e4 * notional, 0.0)  # quote currency

    pnl_total = float(np.nansum(orders["pnl"])) if orders["pnl"] is not None else None
    drag_total = float(drag.sum())
    if pnl_total is not None and pnl_total > 0:
        share = drag_total / pnl_total
        verdict = "OVERSTATED" if share > args.tolerance else "CONSISTENT"
    else:
        share = None
        verdict = "REPORTED"

    hours = ((r["t_arr"] // US_PER_H) % 24).astype(int)
    by_hour = [{"hour": h, "orders": int(m.sum()), "cost_bps": _stats(cost[m])}
               for h in range(24) for m in [taker & (hours == h)] if m.any()]

    by_vol = []
    rng = r["range_bps"]
    ok = taker & np.isfinite(rng) & np.isfinite(cost)
    if ok.sum() >= 3:
        cuts = np.quantile(rng[ok], [1 / 3, 2 / 3])
        tercile = np.where(rng <= cuts[0], 0, np.where(rng <= cuts[1], 1, 2))
        edges = [float(np.nanmin(rng[ok])), float(cuts[0]), float(cuts[1]), float(np.nanmax(rng[ok]))]
        for q, name in enumerate(("low", "mid", "high")):
            m = ok & (tercile == q)
            by_vol.append({"tercile": name, "range_bps": [edges[q], edges[q + 1]], "orders": int(m.sum()),
                           "cost_bps": _stats(cost[m])})

    latency = [{"latency_ms": ms, "cost_bps": _stats(sweep[ms][taker])} for ms in LATENCY_SWEEP_MS]

    t = orders["t"]
    return {
        "verdict": verdict,
        "orders_file": args.orders,
        "run_name": args.run_name or os.path.basename(args.orders),
        "source": source,
        "window_utc": [int(t.min()), int(t.max())],
        "tick": tick,
        "latency_ms": args.latency_ms,
        "tolerance": args.tolerance,
        "baseline": orders["baseline_source"],
        "has_fill_px": orders["has_fill_px"],
        "counts": {
            "orders": int(len(t)),
            "taker": int(taker.sum()),
            "resting": int((st == sim.RESTING).sum()),
            "no_data": int((st == sim.NO_DATA).sum()),
            "beyond_touch": int(r["beyond_touch"].sum()),
            "partially_unfilled": int((taker & (r["unfilled"] > 1e-12)).sum()),
        },
        "cost_bps": _stats(cost[taker]),
        "drag": {"quote": drag_total, "pnl": pnl_total, "share_of_pnl": share},
        "latency_sweep": latency,
        "by_hour_utc": by_hour,
        "by_volatility": by_vol,
        "worst_ids": orders["id"][taker][np.argsort(-np.nan_to_num(cost[taker], nan=-np.inf))][:20].tolist(),
    }
