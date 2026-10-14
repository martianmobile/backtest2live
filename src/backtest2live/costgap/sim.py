"""Taker pricing on an L1 book: what a market order, or a marketable limit, pays.

Deterministic, numpy only. Times are int64 microseconds, prices floats.

Per order, at arrival (t + latency):
  - the opposite touch is read from the last book row at or before arrival.
  - a limit order that does not cross the touch is `resting` (that is fill-gap's
    job); one that crosses is priced as a taker up to its limit.
  - the displayed size at the touch fills at the touch price. Size beyond it is
    priced one tick worse per displayed-size slice (a flat-depth floor: L1 cannot
    see the levels behind the touch, so this is the least impact consistent with
    the feed, not a measurement). A limit caps the walk; size past the limit is
    unfilled.
  - cost = signed (fill VWAP - baseline) / baseline, in bps. Positive means the
    order paid more than the backtest booked. The baseline is the backtest's own
    fill price when the log has one, else the mid at arrival.
"""

import numpy as np

RESTING, NO_DATA, TAKER = 0, 1, 2
STATUS_NAMES = {RESTING: "resting", NO_DATA: "no_data", TAKER: "taker"}


def price_takers(book, orders, tick, latency_us=0):
    """orders: dict t, buy (bool), size, limit (float, nan = market), fill_px (float, nan = none).
    Returns per-order arrays: status, t_arr, baseline, vwap, filled, beyond_touch (bool), cost_bps."""
    n = len(orders["t"])
    t_arr = orders["t"] + int(latency_us)
    bt = book["t"]
    i = np.searchsorted(bt, t_arr, "right") - 1
    covered = (i >= 0) & (t_arr <= bt[-1]) if len(bt) else np.zeros(n, bool)
    ic = np.clip(i, 0, max(len(bt) - 1, 0))

    buy = orders["buy"]
    sgn = np.where(buy, 1.0, -1.0)
    touch_px = np.where(buy, book["ask_px"][ic], book["bid_px"][ic])
    touch_qty = np.where(buy, book["ask_qty"][ic], book["bid_qty"][ic])
    mid = (book["ask_px"][ic] + book["bid_px"][ic]) / 2.0

    limit = orders["limit"]
    has_limit = np.isfinite(limit)
    # a buy limit below the ask (or a sell above the bid) rests; it does not take
    crosses = ~has_limit | (sgn * (limit - touch_px) >= -1e-12)

    status = np.full(n, NO_DATA, dtype=np.int8)
    status[covered & ~crosses] = RESTING
    status[covered & crosses] = TAKER
    taker = status == TAKER

    size = orders["size"].astype(np.float64)
    q1 = np.minimum(size, touch_qty)
    rem = np.maximum(size - touch_qty, 0.0)
    # levels of displayed size behind the touch needed for the remainder (flat-depth floor)
    with np.errstate(divide="ignore", invalid="ignore"):
        levels = np.where(rem > 0, np.ceil(rem / np.where(touch_qty > 0, touch_qty, np.nan)), 0.0)
    levels = np.nan_to_num(levels, nan=0.0, posinf=0.0)
    # a limit caps how many ticks past the touch the walk may go
    max_levels = np.where(has_limit, np.floor(sgn * (limit - touch_px) / tick + 1e-9), np.inf)
    walk = np.minimum(levels, np.maximum(max_levels, 0.0))
    filled_rem = np.where(levels > 0, np.minimum(rem, walk * touch_qty), 0.0)
    # remainder VWAP: slices at touch + 1..walk ticks, each touch_qty, last one partial
    rem_cost = np.zeros(n)
    full = np.floor(np.where(touch_qty > 0, filled_rem / np.where(touch_qty > 0, touch_qty, 1.0), 0.0))
    partial = filled_rem - full * touch_qty
    rem_cost = touch_qty * tick * full * (full + 1) / 2.0 + partial * tick * (full + 1)  # ticks × qty
    filled = q1 + filled_rem
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(filled > 0, touch_px + sgn * rem_cost / np.where(filled > 0, filled, 1.0), np.nan)

    baseline = np.where(np.isfinite(orders["fill_px"]), orders["fill_px"], mid)
    with np.errstate(divide="ignore", invalid="ignore"):
        cost_bps = np.where(taker & (filled > 0), sgn * (vwap - baseline) / baseline * 1e4, np.nan)

    return {
        "status": status,
        "t_arr": t_arr,
        "baseline": np.where(taker, baseline, np.nan),
        "vwap": np.where(taker, vwap, np.nan),
        "filled": np.where(taker, filled, 0.0),
        "unfilled": np.where(taker, size - filled, 0.0),
        "beyond_touch": taker & (rem > 0),
        "cost_bps": cost_bps,
    }
