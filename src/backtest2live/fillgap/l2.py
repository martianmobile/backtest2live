"""Replay an L2 order-book stream (snapshot + deltas) into top-of-book rows.

Two newline-JSON day-file formats are recognised per line:

- Bybit public archive (``quote-saver.bycsi.com/orderbook/<category>/<SYMBOL>/<date>_<SYMBOL>_ob500.data``):
  ``{"type": "snapshot"|"delta", "ts": ms, "data": {"b": [[px, sz], ...], "a": [...]}}``
- OKX historical-data portal ``books`` stream:
  ``{"action": "snapshot"|"update", "ts": ms, "bids": [[px, sz, ...], ...], "asks": [...]}``
- A sampled-snapshot CSV ``timestamp,bids,asks`` (ms, JSON level lists), e.g. snapshots
  sampled from an OKX ``books`` file; each row is a full snapshot.

A level with size 0 is removed. One row is emitted whenever the best bid or
ask (price or displayed size) changes, which is the bookTicker shape the
evaluators already consume. With ``depth=K`` each row also carries the K best
levels per side as they stand at that change (cost-gap walks them): offsets
from the touch and sizes, float32, NaN-padded. The file is streamed line by line.
"""

import bisect
import gzip
import io
import itertools
import json
import os
import zipfile

import numpy as np


def _open_lines(path):
    """Yield text lines from a .data, .data.zip, .gz or plain file."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            with z.open(z.namelist()[0]) as fh:
                for line in io.TextIOWrapper(fh, encoding="utf-8"):
                    yield line
    elif path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                yield line
    else:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                yield line


def _csv_snapshots(lines):
    """Rows of a ``timestamp,bids,asks`` CSV → (ts_ms, True, bids, asks)."""
    import csv
    reader = csv.reader(lines)
    header = next(reader, None)
    if not header or [h.strip().lower() for h in header[:3]] != ["timestamp", "bids", "asks"]:
        raise SystemExit("error: L2 CSV must have the columns timestamp,bids,asks")
    for row in reader:
        if len(row) < 3:
            continue
        yield int(float(row[0])), True, json.loads(row[1]), json.loads(row[2])


def _event(obj):
    """Normalise one message to (ts_ms, is_snapshot, bids, asks) or None."""
    if "data" in obj and "topic" in obj:                      # Bybit
        d = obj["data"]
        return int(obj["ts"]), obj.get("type") == "snapshot", d.get("b", []), d.get("a", [])
    if "action" in obj and ("bids" in obj or "asks" in obj):  # OKX
        return int(obj["ts"]), obj.get("action") == "snapshot", obj.get("bids", []), obj.get("asks", [])
    if "data" in obj and isinstance(obj["data"], list):       # OKX wrapped: {"arg":..., "action":..., "data":[{...}]}
        d = obj["data"][0]
        return int(d["ts"]), obj.get("action") == "snapshot", d.get("bids", []), d.get("asks", [])
    return None


DEPTH = 20             # levels per side kept for cost-gap
CACHE_VERSION = "l1v2"  # v2 adds the depth arrays; bump when the cached arrays change
DEPTH_KEYS = ("bid_lv_off", "bid_lv_qty", "ask_lv_off", "ask_lv_qty")


class _Levels:
    """Growable float32 (rows, K) buffers: a Python list per row would cost ~2 GB a day."""

    def __init__(self, k):
        self.k, self.n, self.bufs = k, 0, [np.full((65_536, k), np.nan, np.float32) for _ in DEPTH_KEYS]

    def add(self, b_off, b_qty, a_off, a_qty):
        if self.n == len(self.bufs[0]):
            self.bufs = [np.concatenate([b, np.full_like(b, np.nan)]) for b in self.bufs]
        for buf, v in zip(self.bufs, (b_off, b_qty, a_off, a_qty)):
            buf[self.n, :len(v)] = v
        self.n += 1

    def arrays(self):
        return {k: b[:self.n] for k, b in zip(DEPTH_KEYS, self.bufs)}


def replay_l1(lines, depth=0):
    """Stream L2 messages → dict of int64 µs times and float best bid/ask price and size
    (plus the DEPTH_KEYS arrays when depth > 0)."""
    bids, asks = {}, {}
    bsorted, asorted = [], []  # ascending prices, kept only when depth > 0
    lv = _Levels(depth) if depth else None
    best_b = best_a = None
    rows_t, rows = [], []
    last = None
    lines = iter(lines)
    first = next(lines, "")
    if first.lstrip().lower().startswith("timestamp,"):
        events = _csv_snapshots(itertools.chain([first], lines))
    else:
        def _json_events(src):
            for line in src:
                line = line.strip()
                if line:
                    ev = _event(json.loads(line))
                    if ev is not None:
                        yield ev
        events = _json_events(itertools.chain([first], lines))
    for ts, snap, b_lv, a_lv in events:
        if snap:
            bids = {float(p): float(s) for p, s, *_ in b_lv if float(s) > 0}
            asks = {float(p): float(s) for p, s, *_ in a_lv if float(s) > 0}
            best_b = max(bids) if bids else None
            best_a = min(asks) if asks else None
            if lv:
                bsorted, asorted = sorted(bids), sorted(asks)
        else:
            for p, s, *_ in b_lv:
                p, s = float(p), float(s)
                if s <= 0:
                    if bids.pop(p, None) is not None:
                        if lv:
                            del bsorted[bisect.bisect_left(bsorted, p)]
                        if p == best_b:
                            best_b = max(bids) if bids else None
                else:
                    if lv and p not in bids:
                        bisect.insort(bsorted, p)
                    bids[p] = s
                    if best_b is None or p > best_b:
                        best_b = p
            for p, s, *_ in a_lv:
                p, s = float(p), float(s)
                if s <= 0:
                    if asks.pop(p, None) is not None:
                        if lv:
                            del asorted[bisect.bisect_left(asorted, p)]
                        if p == best_a:
                            best_a = min(asks) if asks else None
                else:
                    if lv and p not in asks:
                        bisect.insort(asorted, p)
                    asks[p] = s
                    if best_a is None or p < best_a:
                        best_a = p
        if best_b is None or best_a is None:
            continue
        top = (best_b, bids[best_b], best_a, asks[best_a])
        if top != last:
            rows_t.append(ts)
            rows.append(top)
            last = top
            if lv:
                kb = bsorted[:-depth - 1:-1]  # best first
                ka = asorted[:depth]
                lv.add([p - best_b for p in kb], [bids[p] for p in kb],
                       [p - best_a for p in ka], [asks[p] for p in ka])
    if not rows:
        raise SystemExit("error: no top-of-book rows could be replayed from the L2 file")
    arr = np.asarray(rows, dtype=np.float64)
    out = {"t": np.asarray(rows_t, dtype=np.int64) * 1000, "bid_px": arr[:, 0], "bid_qty": arr[:, 1],
           "ask_px": arr[:, 2], "ask_qty": arr[:, 3]}
    if lv:
        out.update(lv.arrays())
    return out


L1_KEYS = ("t", "bid_px", "bid_qty", "ask_px", "ask_qty")


def l1_from_file(path, cache_path=None, log=print, with_depth=False):
    """Replay once and cache the arrays as .npz. With with_depth (cost-gap) the replay keeps DEPTH
    levels per side and the cache is compressed; without it only top-of-book is kept, so fill-gap
    pays nothing for depth. A depth cache also serves an L1 request (npz members load lazily)."""
    keys = L1_KEYS + (DEPTH_KEYS if with_depth else ())
    if cache_path and os.path.exists(cache_path):
        z = np.load(cache_path)
        return {k: z[k] for k in keys}
    depth = DEPTH if with_depth else 0
    log(f"replaying L2 → top-of-book{' and depth' if depth else ''}: {os.path.basename(path)}")
    book = replay_l1(_open_lines(path), depth=depth)
    if cache_path:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".part.npz"
        (np.savez_compressed if depth else np.savez)(tmp, **book)
        os.replace(tmp, cache_path)  # atomic: a killed run never leaves a half-written cache
    return {k: book[k] for k in keys}
