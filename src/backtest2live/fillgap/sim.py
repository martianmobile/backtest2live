"""Queue-proxy fill simulation for resting limit orders on L1 book + trade prints.

Deterministic, numpy only. All times are int64 microseconds; all prices are
int64 ticks. Buy logic is written once: sell orders are mirrored by negating
prices, so "the bid" is the order's own side and "below P" means through it.

Per order, in the window (active, end]:
  - marketable: the limit crosses the opposite touch at arrival. A taker
    order, out of scope for the maker comparison.
  - queue ahead: 0 if the order improves the touch; the displayed size if it
    joins the touch; if it rests behind the touch, the displayed size when the
    touch first reaches its price (0 if the touch jumps past it).
  - proxy fill: passive-side volume printed at exactly its price reaches
    queue ahead + own size, or a passive-side trade prints through its price.
  - touch fill (the naive backtest rule): any passive-side print at or
    through its price after arrival.
"""

import numpy as np

# status codes
NO_DATA, MARKETABLE, NEVER_REACHED, ACTIVE = 0, 1, 2, 3
STATUS_NAMES = {NO_DATA: "no_data", MARKETABLE: "marketable",
                NEVER_REACHED: "never_reached", ACTIVE: "active"}


def _side_arrays(book, trades, buy):
    s = 1 if buy else -1
    own_px = book["bid_px"] if buy else -book["ask_px"]
    own_qty = book["bid_qty"] if buy else book["ask_qty"]
    opp_px = book["ask_px"] if buy else -book["bid_px"]
    # A resting buy is hit by aggressive sells, a resting sell by aggressive buys.
    hit = trades["sell_aggr"] if buy else ~trades["sell_aggr"]
    return own_px, own_qty, opp_px, trades["t"][hit], s * trades["px"][hit], trades["qty"][hit]


def _first_through(ht, hp, t_from, t_to, P, idx):
    """For orders idx: time of the first hit trade with price < P in (t_from, t_to], else -1."""
    out = np.full(len(P), -1, dtype=np.int64)
    a = np.searchsorted(ht, t_from, "right")
    b = np.searchsorted(ht, t_to, "right")
    for j in idx:
        if b[j] <= a[j]:
            continue
        seg = hp[a[j]:b[j]]
        m = seg < P[j]
        if m.any():
            out[j] = ht[a[j] + int(m.argmax())]
    return out


def simulate_side(book, trades, t_arr, t_end, P_raw, size, buy):
    """Simulate orders of one side. Returns dict of per-order arrays."""
    n = len(t_arr)
    own_px, own_qty, opp_px, ht, hp, hq = _side_arrays(book, trades, buy)
    P = P_raw if buy else -P_raw
    bt = book["t"]

    status = np.full(n, NO_DATA, dtype=np.int8)
    queue = np.zeros(n, dtype=np.float64)
    t_act = np.full(n, -1, dtype=np.int64)

    i = np.searchsorted(bt, t_arr, "right") - 1
    covered = (i >= 0) & (t_arr <= bt[-1]) & (t_end >= t_arr) if len(bt) else np.zeros(n, bool)
    ic = np.clip(i, 0, max(len(bt) - 1, 0))

    marketable = covered & (P >= opp_px[ic])
    improve = covered & ~marketable & (P > own_px[ic])
    at_touch = covered & ~marketable & (P == own_px[ic])
    behind = covered & ~marketable & (P < own_px[ic])

    status[marketable] = MARKETABLE
    status[improve | at_touch] = ACTIVE
    t_act[improve | at_touch] = t_arr[improve | at_touch]
    queue[at_touch] = own_qty[ic[at_touch]]

    # Behind the touch: wait until the touch comes to the order's price.
    i_end = np.searchsorted(bt, t_end, "right") - 1
    status[behind] = NEVER_REACHED
    for j in np.flatnonzero(behind):
        seg = own_px[ic[j] + 1:i_end[j] + 1]
        m = seg <= P[j]
        if m.any():
            k = ic[j] + 1 + int(m.argmax())
            status[j] = ACTIVE
            t_act[j] = bt[k]
            queue[j] = own_qty[k] if own_px[k] == P[j] else 0.0

    active = status == ACTIVE
    live = covered & ~marketable
    idx_live = np.flatnonzero(live)

    # --- volume printed at exactly P, sorted by (price, time) ---
    t_fill_at = np.full(n, -1, dtype=np.int64)
    t_touch_at = np.full(n, -1, dtype=np.int64)
    if len(ht):
        t0 = min(int(ht.min()), int(t_arr.min())) - 1
        span = max(int(ht.max()), int(t_end.max())) - t0 + 2
        B = 1 << int(span).bit_length()
        pmin = min(int(hp.min()), int(P.min()))
        pmax = max(int(hp.max()), int(P.max()))
        if (pmax - pmin + 1) * B >= (1 << 62):
            raise SystemExit("error: price range × time span too large for one pass; split the input by day")
        key = (hp - pmin) * B + (ht - t0)
        o = np.argsort(key, kind="stable")
        key, ts, cs = key[o], ht[o], np.r_[0.0, np.cumsum(hq[o])]
        base = (P - pmin) * B - t0

        # touch rule: any print at P after arrival
        lo_n = np.searchsorted(key, base + t_arr, "right")
        hi = np.searchsorted(key, base + t_end, "right")
        has = live & (hi > lo_n)
        t_touch_at[has] = ts[lo_n[has]]

        # proxy: cumulative volume at P after activation covers queue + own size
        ta = np.where(active, t_act, t_end)
        lo = np.searchsorted(key, base + ta, "right")
        k = np.searchsorted(cs, cs[lo] + queue + size - 1e-12, "left")
        ok = active & (k > lo) & (k <= hi)
        t_fill_at[ok] = ts[k[ok] - 1]

    # --- prints through P (the level was cleared) ---
    t_through = _first_through(ht, hp, t_arr, t_end, P, idx_live)

    def first(a, b):
        both = (a >= 0) & (b >= 0)
        return np.where(both, np.minimum(a, b), np.maximum(a, b))

    t_touch = first(t_touch_at, t_through)
    # A print through P clears every order resting at P since arrival, so the
    # through leg applies from t_arr, also to orders the book never showed at
    # the touch (the sweep and the book row can share a ms, or the print can
    # lead the row).
    t_through_proxy = np.where(live, t_through, -1)
    t_proxy = first(t_fill_at, t_through_proxy)
    status[(status == NEVER_REACHED) & (t_proxy >= 0)] = ACTIVE

    return {"status": status, "queue": queue, "t_act": t_act,
            "touch": t_touch >= 0, "t_touch": t_touch,
            "proxy": t_proxy >= 0, "t_proxy": t_proxy}


def simulate(book, trades, orders, latency_us=0):
    """orders: dict t, t_end, buy (bool), px (ticks), size. Returns per-order arrays in input order."""
    n = len(orders["t"])
    out = {
        "status": np.zeros(n, np.int8), "queue": np.zeros(n), "t_act": np.full(n, -1, np.int64),
        "touch": np.zeros(n, bool), "t_touch": np.full(n, -1, np.int64),
        "proxy": np.zeros(n, bool), "t_proxy": np.full(n, -1, np.int64),
    }
    t_arr = orders["t"] + int(latency_us)
    for buy in (True, False):
        m = orders["buy"] == buy
        if not m.any():
            continue
        r = simulate_side(book, trades, t_arr[m], orders["t_end"][m], orders["px"][m],
                          orders["size"][m], buy)
        for k, v in r.items():
            out[k][m] = v
    out["t_arr"] = t_arr
    return out


def trailing_range_bps(book, t_query, window_us=300_000_000, bin_us=60_000_000):
    """Trailing realized range of the mid (max-min over the previous 5 one-minute bins, bps of the min)."""
    bt = book["t"]
    mid = (book["bid_px"] + book["ask_px"]) / 2.0
    t0 = bt[0]
    b = (bt - t0) // bin_us
    nb = int(b[-1]) + 1
    mx = np.full(nb, np.nan)
    mn = np.full(nb, np.nan)
    np.fmax.at(mx, b, mid)
    np.fmin.at(mn, b, mid)
    # carry the last value through empty bins
    for arr in (mx, mn):
        idx = np.where(np.isnan(arr), 0, np.arange(nb))
        np.maximum.accumulate(idx, out=idx)
        arr[:] = arr[idx]
    w = int(window_us // bin_us)
    rmx, rmn = _rolling(mx, w, np.fmax), _rolling(mn, w, np.fmin)
    rng = (rmx - rmn) / rmn * 1e4
    q = np.clip((t_query - t0) // bin_us - 1, 0, nb - 1)  # the window before the order
    return np.where(t_query >= t0, rng[q], np.nan)


def _rolling(a, w, f):
    """Trailing w-bin reduction: out[i] = f over a[i-w+1..i]."""
    out = a.copy()
    for s in range(1, w):
        out[s:] = f(out[s:], a[:-s])
    return out
