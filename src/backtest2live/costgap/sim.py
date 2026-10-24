"""Taker pricing on an L1 book: what a market order, or a marketable limit, pays.

Deterministic, numpy only. Times are int64 microseconds, prices floats.

Per order, at arrival (t + latency):
  - the opposite touch is read from the last book row at or before arrival.
  - a limit order that does not cross the touch is `resting` (that is fill-gap's
    job); one that crosses is priced as a taker up to its limit.
  - the displayed size at the touch fills at the touch price. Behind it:
    - with L2 depth in the book (the l2.DEPTH_KEYS arrays: an L2 archive or file),
      the order walks the real levels recorded at the last top-of-book change;
      size past the deepest recorded level falls back to the floor below,
      starting from that level.
    - with top-of-book only, size beyond the touch is priced one tick worse per
      displayed-size slice (a flat-depth floor: L1 cannot see the levels behind
      the touch, so this is the least impact consistent with the feed, not a
      measurement).
    A limit caps the walk; size past the limit is unfilled.
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
    rem = np.maximum(size - touch_qty, 0.0)
    if "ask_lv_qty" in book:
        filled, value, past_depth = _walk_depth(book, ic, buy, sgn, size, touch_px, limit, has_limit, tick)
    else:
        q1 = np.minimum(size, touch_qty)
        f2, v2 = _floor_walk(touch_px, touch_qty, rem, limit, has_limit, sgn, tick)
        filled, value, past_depth = q1 + f2, q1 * touch_px + v2, np.zeros(n, bool)
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(filled > 0, value / np.where(filled > 0, filled, 1.0), np.nan)

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
        "past_depth": taker & past_depth,
        "cost_bps": cost_bps,
    }


def _floor_walk(start_px, slice_qty, rem, limit, has_limit, sgn, tick):
    """Flat-depth floor behind start_px: `rem` filled in slices of slice_qty, each one tick worse,
    capped by the limit. Returns (filled, value = sum of price x qty)."""
    with np.errstate(divide="ignore", invalid="ignore"):
        levels = np.where(rem > 0, np.ceil(rem / np.where(slice_qty > 0, slice_qty, np.nan)), 0.0)
    levels = np.nan_to_num(levels, nan=0.0, posinf=0.0)
    # a limit caps how many ticks past start_px the walk may go
    max_levels = np.where(has_limit, np.floor(sgn * (limit - start_px) / tick + 1e-9), np.inf)
    walk = np.minimum(levels, np.maximum(max_levels, 0.0))
    filled = np.where(levels > 0, np.minimum(rem, walk * slice_qty), 0.0)
    # slices at start + 1..walk ticks, each slice_qty, the last one partial
    full = np.floor(np.where(slice_qty > 0, filled / np.where(slice_qty > 0, slice_qty, 1.0), 0.0))
    partial = filled - full * slice_qty
    ticks_x_qty = slice_qty * full * (full + 1) / 2.0 + partial * (full + 1)
    return filled, filled * start_px + sgn * tick * ticks_x_qty


def _walk_depth(book, ic, buy, sgn, size, touch_px, limit, has_limit, tick):
    """Walk the recorded levels (level 0 is the touch), then the floor past the deepest one."""
    off = np.where(buy[:, None], book["ask_lv_off"][ic], book["bid_lv_off"][ic]).astype(np.float64)
    qty = np.where(buy[:, None], book["ask_lv_qty"][ic], book["bid_lv_qty"][ic]).astype(np.float64)
    present = np.isfinite(qty) & (qty > 0)
    px = touch_px[:, None] + np.nan_to_num(off)  # offsets are signed: asks above, bids below
    allowed = present & (~has_limit[:, None] | (sgn[:, None] * (limit[:, None] - px) >= -1e-9))
    q = np.where(allowed, qty, 0.0)
    before = np.cumsum(q, axis=1) - q
    take = np.clip(size[:, None] - before, 0.0, q)
    filled = take.sum(axis=1)
    value = (take * px).sum(axis=1)
    # past the deepest recorded level: the floor, from that level's price and size
    k = present.shape[1]
    last = k - 1 - np.argmax(present[:, ::-1], axis=1)
    rows = np.arange(len(size))
    last_px, last_qty = px[rows, last], np.where(present[rows, last], qty[rows, last], 0.0)
    rem = np.maximum(size - filled, 0.0)
    # only when every recorded level was taken; a limit that stopped the walk earlier stops it here too
    exhausted = (rem > 1e-12) & (allowed.sum(axis=1) == present.sum(axis=1))
    f2, v2 = _floor_walk(last_px, last_qty, np.where(exhausted, rem, 0.0), limit, has_limit, sgn, tick)
    return filled + f2, value + v2, exhausted
