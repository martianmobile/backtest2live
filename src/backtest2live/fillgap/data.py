"""Inputs for fill-gap: the user's order log, and L1 book + trade prints.

Book and trades come from a loader: the Binance public archive (USDT-M
futures, downloaded once and cached) or the user's own CSVs. Every loader
returns the same normalized arrays: times in int64 microseconds, prices as
floats (converted to ticks by the caller), sizes in base units.
"""

import hashlib
import os
import urllib.request
import zipfile
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pandas as pd

ORDER_ALIASES = {
    "t": ("ts", "timestamp", "time", "datetime", "created_at", "submit_time", "t"),
    "side": ("side", "direction", "dir"),
    "px": ("price", "px", "limit_price", "limit"),
    "size": ("size", "qty", "quantity", "amount", "sz"),
    "filled": ("filled", "is_filled", "fill", "backtest_filled"),
    "fill_qty": ("fill_qty", "filled_qty", "executed_qty"),
    "t_end": ("end_ts", "cancel_ts", "expire_ts", "cancel_time", "end_time", "expiry"),
    "pnl": ("pnl", "pnl_usd", "realized_pnl", "profit"),
    "id": ("order_id", "id", "client_order_id"),
}
BOOK_ALIASES = {
    "t": ("ts", "timestamp", "time", "transaction_time", "event_time", "t"),
    "bid_px": ("bid_px", "bid_price", "best_bid_price", "bid"),
    "bid_qty": ("bid_qty", "bid_size", "best_bid_qty", "bid_sz"),
    "ask_px": ("ask_px", "ask_price", "best_ask_price", "ask"),
    "ask_qty": ("ask_qty", "ask_size", "best_ask_qty", "ask_sz"),
}
TRADE_ALIASES = {
    "t": ("ts", "timestamp", "time", "transact_time", "trade_time", "t"),
    "px": ("price", "px"),
    "qty": ("qty", "quantity", "size", "amount"),
    "is_buyer_maker": ("is_buyer_maker", "buyer_maker", "m"),
    "aggr_side": ("side", "aggressor_side", "taker_side"),
}

BUY_WORDS = {"buy", "b", "bid", "long", "1", "+1"}
SELL_WORDS = {"sell", "s", "ask", "short", "-1"}
TRUE_WORDS = {"true", "t", "1", "yes", "y", "1.0"}
FALSE_WORDS = {"false", "f", "0", "no", "n", "0.0", ""}


def _resolve(df, aliases, required, what):
    cols = {c.lower().strip(): c for c in df.columns}
    out = {}
    for role, names in aliases.items():
        for n in names:
            if n in cols:
                out[role] = cols[n]
                break
    missing = [r for r in required if r not in out]
    if missing:
        want = "; ".join(f"{r}: one of {', '.join(aliases[r])}" for r in missing)
        raise SystemExit(f"error: {what} is missing columns ({want}). Found: {', '.join(df.columns)}")
    return out


def to_us(col):
    """Epoch s/ms/us/ns numbers or ISO-8601 strings → int64 microseconds UTC."""
    s = pd.Series(col)
    if pd.api.types.is_numeric_dtype(s):
        v = s.to_numpy(dtype=np.float64)
        mag = np.nanmedian(np.abs(v)) if len(v) else 0
        if mag > 1e17:
            f = 1e-3      # ns
        elif mag > 1e14:
            f = 1.0       # us
        elif mag > 1e11:
            f = 1e3       # ms
        else:
            f = 1e6       # s
        return np.rint(v * f).astype(np.int64)
    parsed = pd.to_datetime(s, utc=True, errors="coerce")
    if parsed.isna().any():
        bad = s[parsed.isna()].iloc[0]
        raise SystemExit(f"error: unparseable timestamp {bad!r}")
    # resolution-independent (pandas 2 may parse to s/ms/us/ns)
    return ((parsed - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(microseconds=1)).to_numpy(dtype=np.int64)


def _bool(col, what):
    s = pd.Series(col).astype(str).str.strip().str.lower()
    bad = ~s.isin(TRUE_WORDS | FALSE_WORDS)
    if bad.any():
        raise SystemExit(f"error: {what} must be true/false or 1/0; got {s[bad].iloc[0]!r}")
    return s.isin(TRUE_WORDS).to_numpy()


def load_orders(path, max_rest_s):
    try:
        df = pd.read_csv(path)
    except FileNotFoundError:
        raise SystemExit(f"error: file not found: {path!r}")
    except (pd.errors.EmptyDataError, pd.errors.ParserError) as e:
        raise SystemExit(f"error: could not read {path!r}: {e}")
    if df.empty:
        raise SystemExit(f"error: no data rows in {path!r}")
    c = _resolve(df, ORDER_ALIASES, ("t", "side", "px", "size"), "order log")

    side = df[c["side"]].astype(str).str.strip().str.lower()
    unknown = ~side.isin(BUY_WORDS | SELL_WORDS)
    if unknown.any():
        raise SystemExit(f"error: side must be buy/sell; got {side[unknown].iloc[0]!r}")

    t = to_us(df[c["t"]])
    if "t_end" in c:
        t_end = to_us(df[c["t_end"]])
        end_source = c["t_end"]
    else:
        t_end = t + int(max_rest_s * 1e6)
        end_source = f"--max-rest {max_rest_s:g}s"

    if "filled" in c:
        claimed = _bool(df[c["filled"]], c["filled"])
    elif "fill_qty" in c:
        claimed = pd.to_numeric(df[c["fill_qty"]], errors="coerce").fillna(0).to_numpy() > 0
    else:
        claimed = None

    return {
        "t": t,
        "t_end": t_end,
        "buy": side.isin(BUY_WORDS).to_numpy(),
        "px_f": pd.to_numeric(df[c["px"]], errors="raise").to_numpy(np.float64),
        "size": pd.to_numeric(df[c["size"]], errors="raise").to_numpy(np.float64),
        "claimed": claimed,
        "pnl": pd.to_numeric(df[c["pnl"]], errors="coerce").to_numpy(np.float64) if "pnl" in c else None,
        "id": df[c["id"]].astype(str).to_numpy() if "id" in c else np.arange(len(df)).astype(str),
        "columns": c,
        "end_source": end_source,
    }


# --- user-supplied book and trades ------------------------------------------

def load_user_book(path):
    df = pd.read_csv(path)
    c = _resolve(df, BOOK_ALIASES, tuple(BOOK_ALIASES), "book file")
    out = {"t": to_us(df[c["t"]])}
    for k in ("bid_px", "bid_qty", "ask_px", "ask_qty"):
        out[k] = df[c[k]].to_numpy(np.float64)
    return _sort(out)


def load_user_trades(path):
    df = pd.read_csv(path)
    c = _resolve(df, TRADE_ALIASES, ("t", "px", "qty"), "trades file")
    if "is_buyer_maker" in c:
        sell_aggr = _bool(df[c["is_buyer_maker"]], c["is_buyer_maker"])
    elif "aggr_side" in c:
        s = df[c["aggr_side"]].astype(str).str.strip().str.lower()
        sell_aggr = s.isin(SELL_WORDS).to_numpy()
    else:
        raise SystemExit("error: trades file needs the aggressor side: is_buyer_maker (true = seller hit the bid) "
                         "or side (buy/sell = the taker's side)")
    return _sort({"t": to_us(df[c["t"]]), "px": df[c["px"]].to_numpy(np.float64),
                  "qty": df[c["qty"]].to_numpy(np.float64), "sell_aggr": sell_aggr})


def _sort(d):
    o = np.argsort(d["t"], kind="stable")
    return {k: v[o] for k, v in d.items()}


# --- Binance public archive (USDT-M futures) ----------------------------------

BINANCE_ROOT = "https://data.binance.vision/data/futures/um/daily"
BINANCE_BOOKTICKER_LAST = date(2024, 3, 30)  # the archive stops publishing bookTicker after this day


def cache_dir():
    base = os.environ.get("BT2LIVE_CACHE") or os.path.join(
        os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache"), "backtest2live")
    return base


def _fetch(kind, pair, day, offline, log):
    name = f"{pair}-{kind}-{day.isoformat()}.zip"
    url = f"{BINANCE_ROOT}/{kind}/{pair}/{name}"
    path = os.path.join(cache_dir(), "data.binance.vision", "data", "futures", "um", "daily", kind, pair, name)
    if os.path.exists(path):
        return path
    if offline:
        raise SystemExit(f"error: {name} is not cached and --offline is set (cache: {cache_dir()})")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log(f"downloading {url}")
    try:
        with urllib.request.urlopen(url + ".CHECKSUM", timeout=60) as r:
            want = r.read().decode().split()[0]
        tmp = path + ".part"
        h = hashlib.sha256()
        with urllib.request.urlopen(url, timeout=600) as r, open(tmp, "wb") as fh:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                h.update(chunk)
                fh.write(chunk)
    except OSError as e:
        raise SystemExit(f"error: could not download {url}: {e}")
    if h.hexdigest() != want:
        os.remove(tmp)
        raise SystemExit(f"error: checksum mismatch for {name}")
    os.replace(tmp, path)
    return path


def _read_zip_csv(path, usecols, dtype=None):
    with zipfile.ZipFile(path) as z:
        with z.open(z.namelist()[0]) as fh:
            return pd.read_csv(fh, usecols=usecols, dtype=dtype)


def load_binance_um(pair, days, offline=False, log=print):
    """bookTicker + aggTrades for the given UTC days, concatenated."""
    late = [d for d in days if d > BINANCE_BOOKTICKER_LAST]
    if late:
        raise SystemExit(
            f"error: Binance publishes USDT-M bookTicker only through {BINANCE_BOOKTICKER_LAST}; your orders "
            f"reach {max(late)}. Pass your own top-of-book and trades with --book and --trades.")
    books, trades = [], []
    for d in days:
        b = _read_zip_csv(_fetch("bookTicker", pair, d, offline, log),
                          ["best_bid_price", "best_bid_qty", "best_ask_price", "best_ask_qty", "transaction_time"],
                          {"best_bid_price": "f8", "best_bid_qty": "f8", "best_ask_price": "f8",
                           "best_ask_qty": "f8", "transaction_time": "i8"})
        tr = _read_zip_csv(_fetch("aggTrades", pair, d, offline, log),
                           ["price", "quantity", "transact_time", "is_buyer_maker"])
        books.append(b)
        trades.append(tr)
    b = pd.concat(books, ignore_index=True)
    tr = pd.concat(trades, ignore_index=True)
    book = _sort({"t": b["transaction_time"].to_numpy(np.int64) * 1000,
                  "bid_px": b["best_bid_price"].to_numpy(), "bid_qty": b["best_bid_qty"].to_numpy(),
                  "ask_px": b["best_ask_price"].to_numpy(), "ask_qty": b["best_ask_qty"].to_numpy()})
    ibm = tr["is_buyer_maker"]
    if ibm.dtype != bool:
        ibm = ibm.astype(str).str.lower().eq("true")
    trd = _sort({"t": tr["transact_time"].to_numpy(np.int64) * 1000, "px": tr["price"].to_numpy(np.float64),
                 "qty": tr["quantity"].to_numpy(np.float64), "sell_aggr": ibm.to_numpy()})
    return book, trd


def utc_days(t_from_us, t_to_us):
    d0 = datetime.fromtimestamp(t_from_us / 1e6, tz=timezone.utc).date()
    d1 = datetime.fromtimestamp(t_to_us / 1e6, tz=timezone.utc).date()
    return [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]


def infer_tick(book):
    """Smallest price step seen in the book, rounded to a clean decimal."""
    px = np.unique(np.r_[book["bid_px"][:200_000], book["ask_px"][:200_000]])
    d = np.diff(px)
    d = d[d > 0]
    if not len(d):
        raise SystemExit("error: cannot infer the tick size from the book; pass --tick")
    step = float(d.min())
    return float(f"{step:.8g}")  # 0.09999999999 → 0.1
