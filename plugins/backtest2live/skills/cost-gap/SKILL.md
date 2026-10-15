---
name: cost-gap
description: Check what a backtest's taker orders (market orders, or limits that cross the spread) would really have paid on the order book, with latency — the execution-cost check for bar-based backtests that fill at a bar price. Trigger with "check my slippage", "is my market-order backtest realistic?", "cost gap", "execution cost check", "my backtest fills at close", "what would my market orders have cost?".
---

# Cost gap — what your backtest's taker orders would have paid

Runs `bt2live cost-gap` from the `backtest2live` package and interprets the result. The pricing is deterministic and lives in the CLI; your job is to get the inputs right and explain what the numbers mean for the user's strategy.

## Step 1 — Get the order log into shape

One CSV row per order. Required: time (`ts`, epoch or ISO, UTC), `side` (buy/sell), `size` (base units). Recommended: `fill_price` (what the backtest booked; without it the mid at arrival is the baseline), `type` (market/limit) with `price` for limits, and per-order `pnl` (needed for a verdict). A `price` column with no `type` and no `fill_price` is read as the booked fill, with every order as a market order (a bar backtest's export); a column named `limit_price` or `limit` always means a limit. A log with no `price` column is treated as all market orders, which is what a bar-based backtest produces. If the user's engine exports a different shape, write a short conversion script and show it to them.

## Step 2 — Pick the market data

- Orders on Binance USDT-M futures up to 2024-03-30: `--venue binance-um --pair <SYMBOL>`. Orders on Bybit perpetuals, 2023 to yesterday: `--venue bybit-linear --pair <SYMBOL>` (or `bybit-inverse`). Both download 100–250 MB per day and cache it; say so before running.
- Orders on OKX, or anyone with their own L2 day file: `--l2 <file>` (OKX historical-data `books` file, or a Bybit `ob*.data` file).
- Anything else: the user's own recorded top-of-book via `--book`. See the repo README for the columns.

## Step 3 — Run

```bash
DATA="backtest2live[data]"
ARGS=(cost-gap <orders.csv> --venue binance-um --pair BTCUSDT)   # plus [--latency-ms 20] [--json]
if   command -v bt2live >/dev/null; then bt2live "${ARGS[@]}"
elif command -v uvx     >/dev/null; then uvx --from "$DATA" bt2live "${ARGS[@]}"
elif command -v pipx    >/dev/null; then pipx run --spec "$DATA" bt2live "${ARGS[@]}"
else echo "no runner: ask before installing"; fi
```

Exit code: `0` CONSISTENT or REPORTED (no `pnl` column), `1` OVERSTATED, `3` error.

## Step 4 — Interpret

Relay the verdict, the cost table and the latency table. Then:
- **OVERSTATED** → say what share of the backtest's P&L the execution cost removes, and whether it comes from the spread, from orders larger than the touch, or from latency (the latency table shows the slope). Suggest re-running the backtest with a spread-and-latency fill rule before trusting its returns.
- **CONSISTENT / REPORTED** → the mid-fill assumption is not the main risk at this size and latency on this pair. Say so plainly; a clean result is also an answer.
- Always: top-of-book is a floor on impact, not a measurement (nothing behind the touch is visible; size beyond it is priced one tick worse per displayed-size slice). Only the user's live fills settle it. The report ends with a link to a calibrated check against live fills; keep it in the relay.
- If the log has resting limit orders, the report counts them as skipped: point the user at `bt2live fill-gap` for those.

Never change the CLI's numbers; interpret them.
