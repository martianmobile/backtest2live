"""backtest2live · import — convert a backtest engine's native export into the order-log schema.

The order log is the connection between a strategy and the checks: whatever
engine ran the backtest, `fill-gap` and `cost-gap` read one CSV shape. Each
adapter here reads one engine's export and writes that shape.

Order-log schema (`backtest2live.orders/1`), one row per order, columns in this order:

    order_id    the engine's order id
    symbol      the instrument as the engine names it (fill-gap / cost-gap run one --pair at a time)
    ts          arrival time, epoch milliseconds UTC
    side        buy / sell
    type        market / limit
    price       the limit price (blank for a market order)
    size        order size, base units
    filled      1 if the backtest filled any of it, else 0
    fill_qty    size the backtest filled
    fill_price  average price the backtest booked (blank if unfilled)
    end_ts      end of the order's life (fill, cancel or expiry), epoch ms; blank if unknown
                (fill-gap rejects a blank on a limit order: drop orders still open at export)
    pnl         realized P&L the backtest booked on this order, if the engine reports it

Resting orders need `filled` and `end_ts` (fill-gap reads them); market orders
need `fill_price` (cost-gap reads it). Adapters that only see fills (trade
lists) write `filled=1` on every row, which feeds cost-gap but not fill-gap.

Pure standard library, so `bt2live import` runs on a bare Python install.
"""

import csv
import json
import math
import os
import re
import sys
from datetime import datetime, timezone

SCHEMA = "backtest2live.orders/1"
COLUMNS = ("order_id", "symbol", "ts", "side", "type", "price", "size", "filled", "fill_qty", "fill_price",
           "end_ts", "pnl")


# --- shared parsing ---------------------------------------------------------------

_FRAC = re.compile(r"(\.\d{6})\d+")  # fromisoformat takes at most 6 fractional digits


def to_ms(v):
    """Epoch s/ms/us/ns number or ISO-8601 string → int epoch ms UTC; None when blank."""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        x = float(v)
    else:
        s = str(v).strip()
        if s == "" or s.lower() in ("nan", "nat", "none", "null"):
            return None
        try:
            x = float(s)
        except ValueError:
            return _iso_ms(s)
    if not math.isfinite(x):
        return None
    mag = abs(x)
    if mag > 1e17:
        return int(round(x / 1e6))   # ns
    if mag > 1e14:
        return int(round(x / 1e3))   # us
    if mag > 1e11:
        return int(round(x))         # ms
    return int(round(x * 1e3))       # s


def _iso_ms(s):
    t = s.replace("Z", "+00:00").replace("z", "+00:00")
    t = _FRAC.sub(r"\1", t)
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        raise SystemExit(f"error: unparseable timestamp {s!r}")
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)  # engines that write naive times write UTC
    return int(round(d.timestamp() * 1000))


def num(v):
    """Number or None. Accepts strings with thousands separators or a trailing currency."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if math.isfinite(v) else None
    s = str(v).strip().replace(",", "")
    m = re.match(r"^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?", s)
    if not m:
        return None
    x = float(m.group(0))
    return x if math.isfinite(x) else None


def side_of(v):
    s = str(v).strip().lower()
    if s in ("buy", "b", "long", "bid", "1"):
        return "buy"
    if s in ("sell", "s", "short", "ask", "-1"):
        return "sell"
    raise SystemExit(f"error: side must be buy/sell; got {v!r}")


def read_csv(path):
    try:
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows = list(csv.DictReader(fh))
    except FileNotFoundError:
        raise SystemExit(f"error: file not found: {path!r}")
    except (OSError, UnicodeDecodeError, csv.Error) as e:
        raise SystemExit(f"error: could not read {path!r}: {e}")
    if not rows:
        raise SystemExit(f"error: no data rows in {path!r}")
    return [{(k or "").strip().lower(): v for k, v in r.items()} for r in rows]


def read_json(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"error: file not found: {path!r}")
    except (OSError, UnicodeDecodeError, ValueError) as e:
        raise SystemExit(f"error: could not read {path!r} as JSON: {e}")


def pick(row, *names):
    for n in names:
        v = row.get(n)
        if v is not None and str(v).strip() != "":
            return v
    return None


def order(order_id, symbol, ts, side, typ, size, price=None, fill_qty=None, fill_price=None, end_ts=None,
          pnl=None):
    """One schema row. fill_qty None means the adapter only sees fills: filled in full."""
    if ts is None:
        raise SystemExit(f"error: order {order_id!r} has no time")
    if size is None or size <= 0:
        raise SystemExit(f"error: order {order_id!r} has no positive size")
    if typ not in ("market", "limit"):
        raise SystemExit(f"error: order {order_id!r} has type {typ!r}; expected market or limit")
    fq = size if fill_qty is None else fill_qty
    return {
        "order_id": str(order_id), "symbol": symbol or "", "ts": ts, "side": side, "type": typ,
        "price": price if typ == "limit" else None, "size": size,
        "filled": 1 if fq > 0 else 0, "fill_qty": fq, "fill_price": fill_price if fq > 0 else None,
        "end_ts": end_ts, "pnl": pnl,
    }


# --- adapters -----------------------------------------------------------------------
# Each takes a path and returns schema rows. The fixture for each lives in
# tests/fixtures/adapters/ and documents the exact export it expects.

def nautilus(path):
    """NautilusTrader orders report: `trader.generate_orders_report()` written with `.to_csv()`.
    Uses client_order_id, instrument_id, side, type, quantity, price, filled_qty, avg_px, ts_init, ts_last."""
    out = []
    for r in read_csv(path):
        typ = str(pick(r, "type", "order_type") or "").strip().upper()
        if typ in ("MARKET", "MARKET_TO_LIMIT", "STOP_MARKET", "MARKET_IF_TOUCHED", "TRAILING_STOP_MARKET"):
            typ = "market"
        elif typ in ("LIMIT", "STOP_LIMIT", "LIMIT_IF_TOUCHED", "TRAILING_STOP_LIMIT"):
            typ = "limit"
        else:
            raise SystemExit(f"error: Nautilus order type {typ!r} is not a market or limit order")
        fq = num(pick(r, "filled_qty")) or 0.0
        out.append(order(
            pick(r, "client_order_id", "venue_order_id", ""), pick(r, "instrument_id"),
            to_ms(pick(r, "ts_init")), side_of(pick(r, "side", "order_side")), typ, num(pick(r, "quantity")),
            price=num(pick(r, "price")), fill_qty=fq, fill_price=num(pick(r, "avg_px")),
            end_ts=to_ms(pick(r, "ts_last"))))
    return out


LEAN_FILLED = ("filled", "partiallyfilled")
LEAN_FINAL = ("filled", "canceled", "invalid")


def lean(path):
    """QuantConnect LEAN order events: the `<algorithm>-order-events.json` list a backtest writes.
    Events of one orderId are folded into one order: the first event gives arrival time, side,
    quantity and limit price; fill events give the filled size and its average price; the
    last filled/canceled/invalid event ends the order's life."""
    doc = read_json(path)
    events = doc.get("orderEvents", doc) if isinstance(doc, dict) else doc
    if not isinstance(events, list) or not events:
        raise SystemExit("error: LEAN order-events file must be a JSON list of order events")
    by_id = {}
    for e in events:
        e = {str(k)[:1].lower() + str(k)[1:]: v for k, v in e.items()}  # PascalCase in older versions
        oid = e.get("orderId")
        if oid is None:
            raise SystemExit("error: LEAN order event without orderId")
        by_id.setdefault(oid, []).append(e)
    out = []
    for oid, evs in by_id.items():
        evs.sort(key=lambda e: num(e.get("time")) or 0)
        first = evs[0]
        status = [str(e.get("status", "")).lower() for e in evs]
        qty = abs(num(first.get("quantity")) or 0.0)
        limit = num(first.get("limitPrice"))
        fill_q = fill_v = 0.0
        for e, st in zip(evs, status):
            if st in LEAN_FILLED:
                q = abs(num(e.get("fillQuantity")) or 0.0)
                fill_q += q
                fill_v += q * (num(e.get("fillPrice")) or 0.0)
        end = next((e for e, st in zip(reversed(evs), reversed(status)) if st in LEAN_FINAL), None)
        sym = first.get("symbolValue") or first.get("symbol")
        if isinstance(sym, dict):
            sym = sym.get("value") or sym.get("Value")
        out.append(order(
            oid, sym, to_ms(first.get("time")), side_of(first.get("direction")),
            "limit" if limit else "market", qty or fill_q, price=limit, fill_qty=fill_q,
            fill_price=fill_v / fill_q if fill_q else None,
            end_ts=to_ms(end.get("time")) if end else None))
    return out


def backtrader(path):
    """backtrader `bt.analyzers.Transactions`: `get_analysis()` dumped to JSON
    ({datetime: [[amount, price, sid, symbol, value], ...]}), or the same as a CSV
    (date, amount, price, sid, symbol, value). Transactions are fills: every row is
    filled and priced as a market order at the booked price."""
    if path.lower().endswith(".json"):
        doc = read_json(path)
        rows = [{"date": d, "amount": tx[0], "price": tx[1], "symbol": tx[3] if len(tx) > 3 else ""}
                for d, txs in doc.items() for tx in txs]
    else:
        rows = read_csv(path)
    out = []
    for i, r in enumerate(rows):
        amt = num(pick(r, "amount", "size"))
        if not amt:
            continue
        px = num(pick(r, "price"))
        out.append(order(f"bt{i:06d}", pick(r, "symbol", "data"), to_ms(pick(r, "date", "datetime", "dt")),
                         "buy" if amt > 0 else "sell", "market", abs(amt), fill_price=px))
    return out


def vectorbt(path):
    """vectorbt `Portfolio.orders.records_readable` written with `.to_csv()`:
    Order Id, Column, Timestamp, Size, Price, Fees, Side. vectorbt fills each order at
    the price it was given, so every row is a filled market order."""
    out = []
    for r in read_csv(path):
        out.append(order(pick(r, "order id", "id"), pick(r, "column"), to_ms(pick(r, "timestamp", "index")),
                         side_of(pick(r, "side")), "market", num(pick(r, "size")),
                         fill_price=num(pick(r, "price"))))
    return out


_FT_PAIR = re.compile(r"[A-Z0-9]+/[A-Z0-9]+(?::[A-Z0-9]+)?")


def _ft_pair_from_id(order_id):
    """Dry-run order ids carry the pair: dry_run_buy_BTC/USDT:USDT_<suffix>."""
    m = _FT_PAIR.search(str(order_id))
    return m.group(0) if m else None


def freqtrade(path):
    """freqtrade, two exports:
    - a backtest result JSON (`user_data/backtest_results/*.json`, unzipped): every trade's
      `orders` (filled entries and exits), priced at `safe_price`;
    - an orders CSV from a dry-run or live database (the `orders` table): order_id, ft_order_side,
      order_type, price, amount, filled, average, status, order_date, order_filled_date /
      order_update_date. The export has no pair column: the pair is read from `ft_pair` if
      present, else parsed out of the dry-run order_id. In dry-run, `filled` is freqtrade's
      own fill simulator, not a venue's queue."""
    if path.lower().endswith(".json"):
        return _freqtrade_backtest(read_json(path))
    out = []
    for r in read_csv(path):
        oid = pick(r, "order_id", "id")
        pair = pick(r, "ft_pair", "pair") or _ft_pair_from_id(oid)
        typ = str(pick(r, "order_type", "type") or "limit").strip().lower()
        typ = "market" if typ in ("market", "stop_loss", "stoploss") else "limit"
        size = num(pick(r, "amount", "ft_amount"))
        status = str(pick(r, "status") or "").lower()
        fq = num(pick(r, "filled"))
        if fq is None:
            fq = size if status == "closed" else 0.0
        out.append(order(oid, pair, to_ms(pick(r, "order_date", "order_timestamp")),
                         side_of(pick(r, "ft_order_side", "side")), typ, size,
                         price=num(pick(r, "price", "ft_price")), fill_qty=fq,
                         fill_price=num(pick(r, "average", "safe_price", "price")),
                         end_ts=to_ms(pick(r, "order_filled_date", "order_filled_timestamp", "order_update_date"))))
    return out


def _freqtrade_backtest(doc):
    strategies = doc.get("strategy") if isinstance(doc, dict) else None
    if not isinstance(strategies, dict) or not strategies:
        raise SystemExit("error: not a freqtrade backtest result: no 'strategy' section "
                         "(unzip the .zip result and pass the inner .json)")
    if len(strategies) > 1:
        raise SystemExit(f"error: the result holds several strategies ({', '.join(strategies)}); "
                         f"export one at a time")
    trades = next(iter(strategies.values())).get("trades", [])
    out = []
    for ti, t in enumerate(trades):
        orders = t.get("orders") or []
        n_exit = sum(1 for o in orders if not o.get("ft_is_entry"))
        profit = num(t.get("profit_abs"))
        for oi, o in enumerate(orders):
            ts = to_ms(o.get("order_filled_timestamp") or o.get("order_timestamp"))
            # the trade's profit, booked on its exit orders
            exit_pnl = profit / n_exit if profit is not None and not o.get("ft_is_entry") else None
            # Written as market: the result has only the fill time, not when a limit order
            # started resting, so fill-gap has no queue life to replay. Cost-gap reads these.
            out.append(order(f"ft{ti:05d}-{oi}", t.get("pair"), ts, side_of(o.get("ft_order_side")),
                             "market", num(o.get("amount")),
                             fill_price=num(o.get("safe_price")), end_ts=ts, pnl=exit_pnl))
    if not out:
        raise SystemExit("error: the freqtrade result has no trades with orders")
    return out


def hummingbot(path):
    """Hummingbot trade CSV (`data/trades_<strategy>.csv`): one row per fill, with symbol,
    timestamp, order_id, trade_type, order_type, price, amount. Rows of one order_id are
    folded into one filled order at their average price, written as market (fills only)."""
    by_id = {}
    for r in read_csv(path):
        by_id.setdefault(pick(r, "order_id", "exchange_trade_id"), []).append(r)
    out = []
    for oid, fills in by_id.items():
        fills.sort(key=lambda r: to_ms(pick(r, "timestamp")) or 0)
        q = sum(num(pick(r, "amount")) or 0.0 for r in fills)
        v = sum((num(pick(r, "amount")) or 0.0) * (num(pick(r, "price")) or 0.0) for r in fills)
        f0 = fills[0]
        vwap = v / q if q else None
        # Written as market whatever order_type says: the trade CSV starts at the first fill,
        # not the order's arrival, so a limit order's queue life is unknown. Cost-gap reads these.
        out.append(order(oid, pick(f0, "symbol", "market"), to_ms(pick(f0, "timestamp")),
                         side_of(pick(f0, "trade_type", "side")), "market", q, fill_price=vwap,
                         end_ts=to_ms(pick(fills[-1], "timestamp"))))
    return out


ADAPTERS = {
    "nautilus": nautilus,
    "lean": lean,
    "backtrader": backtrader,
    "vectorbt": vectorbt,
    "freqtrade": freqtrade,
    "hummingbot": hummingbot,
}


# --- output ---------------------------------------------------------------------------

def _cell(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:.12g}"
    return str(v)


def write_orders(rows, path):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for r in rows:
            w.writerow([_cell(r[c]) for c in COLUMNS])


def _safe(symbol):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", symbol).strip("-") or "unknown"


def add_arguments(p):
    p.add_argument("engine", choices=sorted(ADAPTERS), help="The engine that wrote the export")
    p.add_argument("input", help="The engine's export (see README for the file each engine writes)")
    p.add_argument("-o", "--output", required=True,
                   help="Order-log CSV to write. With --split, one file per symbol: <output stem>.<SYMBOL>.csv")
    p.add_argument("--symbol", help="Keep only this symbol (as the engine names it)")
    p.add_argument("--split", action="store_true",
                   help="Write one order log per symbol (fill-gap and cost-gap run one --pair at a time)")
    p.add_argument("--json", action="store_true", help="Print a machine-readable summary instead of text")


def run(args):
    rows = ADAPTERS[args.engine](args.input)
    if args.symbol:
        rows = [r for r in rows if r["symbol"] == args.symbol]
        if not rows:
            raise SystemExit(f"error: no orders for symbol {args.symbol!r}")
    rows.sort(key=lambda r: (r["ts"], r["order_id"]))
    symbols = sorted({r["symbol"] for r in rows})

    if args.split:
        stem, ext = os.path.splitext(args.output)
        written = []
        for s in symbols:
            path = f"{stem}.{_safe(s)}{ext or '.csv'}"
            part = [r for r in rows if r["symbol"] == s]
            write_orders(part, path)
            written.append({"symbol": s, "path": path, "orders": len(part)})
    else:
        write_orders(rows, args.output)
        written = [{"symbol": s, "path": args.output, "orders": sum(r["symbol"] == s for r in rows)}
                   for s in symbols]

    summary = {
        "schema": SCHEMA,
        "engine": args.engine,
        "input": args.input,
        "orders": len(rows),
        "filled": sum(r["filled"] for r in rows),
        "market": sum(r["type"] == "market" for r in rows),
        "limit": sum(r["type"] == "limit" for r in rows),
        "files": written,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"{summary['orders']:,} orders from {args.engine} ({summary['limit']:,} limit, "
              f"{summary['market']:,} market; {summary['filled']:,} filled)")
        for f in written:
            print(f"  {f['symbol'] or '(no symbol)'}: {f['orders']:,} → {f['path']}")
        if len(symbols) > 1 and not args.split:
            print(f"note: {len(symbols)} symbols in one file; fill-gap and cost-gap run one --pair at a time, "
                  f"so pass --split or --symbol", file=sys.stderr)
        if summary["limit"] == 0:
            print("note: no limit orders, so there is nothing for fill-gap to re-run; run cost-gap on this log",
                  file=sys.stderr)
    return 0
