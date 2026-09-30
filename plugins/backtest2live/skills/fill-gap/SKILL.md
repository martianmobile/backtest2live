---
name: fill-gap
description: Check whether a backtest's passive (maker/limit) fills survive a queue — re-run the order log against top-of-book and trades and compare the backtest's fill rate with a queue-proxy fill rate. For quant researchers with resting limit orders in their backtest. Trigger with "check my fills", "are my maker fills realistic?", "fill gap", "fill at touch", "queue position check", "would these limit orders have filled?".
---

# Fill gap — do your backtest's passive fills survive a queue?

Runs `bt2live fill-gap` from the `backtest2live` package and interprets the result. The simulation is deterministic and lives in the CLI; your job is to get the inputs right and explain what the numbers mean for the user's strategy.

## Step 1 — Get the order log into shape

One CSV row per order. Required: time (`ts`, epoch or ISO, UTC), `side` (buy/sell), `price`, `size` (base units). Strongly recommended: the backtest's own fill flag (`filled` true/false), the order's end or cancel time (`end_ts`), and per-order `pnl`. If the user's engine exports a different shape, write a short conversion script and show it to them. Don't guess the meaning of a column you can't identify; ask.

## Step 2 — Pick the market data

- Orders on Binance USDT-M futures up to 2024-03-30: `--venue binance-um --pair <SYMBOL>` (downloads about 100 MB per day and caches it; say so before running).
- Anything else: the user's own recorded top-of-book and trades via `--book` and `--trades`. See the repo README for the columns.

## Step 3 — Run

Run the `bt2live` CLI if it is installed; otherwise use an isolated runner from the pinned source. Take the first line that applies:

```bash
SPEC="git+https://github.com/martianmobile/backtest2live@main"   # the package source until it is on PyPI
command -v bt2live >/dev/null && bt2live fill-gap <orders.csv> --venue binance-um --pair BTCUSDT [--latency-ms 20] [--json]
command -v uvx     >/dev/null && uvx --from "${SPEC}#egg=backtest2live[data]" bt2live fill-gap <orders.csv> ...
command -v pipx    >/dev/null && pipx run --spec "${SPEC}#egg=backtest2live[data]" bt2live fill-gap <orders.csv> ...
```

`fill-gap` needs the `[data]` extra (numpy, pandas). If none of the three runners exists, ask before installing anything; `python3 -m venv ~/.bt2live && ~/.bt2live/bin/pip install "${SPEC}#egg=backtest2live[data]"` works everywhere. Never install from any other source than `$SPEC`.

Exit code: `0` CONSISTENT, `1` OVERSTATED, `3` error.

## Step 4 — Interpret

Relay the verdict and the fill-rate table. Then:
- **OVERSTATED** → say what share of the backtest's fills the proxy does not grant, and where they concentrate (volatility tercile, hour). If a `pnl` column was given, name the P&L that sat on those fills. Suggest re-running the backtest with a queue-aware fill rule before trusting its returns.
- **CONSISTENT** → the backtest's fill rule is not the main risk on this data. Note what the proxy cannot see (below).
- Always: the proxy is an estimate from public data, not a measurement. Hidden size, latency and own impact make it generous; cancels ahead make it strict. Only the user's live fills settle it. The report ends with a link to a calibrated check against live fills; keep it in the relay.

Never change the CLI's numbers; interpret them.
