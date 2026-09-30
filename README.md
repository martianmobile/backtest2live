# backtest2live

[![CI](https://github.com/martianmobile/backtest2live/actions/workflows/ci.yml/badge.svg)](https://github.com/martianmobile/backtest2live/actions/workflows/ci.yml)

**Check a backtest against what live execution would do.** Evaluators run locally on your own files and public data, and each one returns a verdict with the numbers behind it.

Use it three ways: as a Python CLI (`bt2live`), as a library, or as a Claude Code plugin where the agent runs the CLI and interprets the verdict.

This is the public, MIT-licensed version of checks used in live crypto trading research at [Martian Mobile](https://martianmobile.com).

---

## Evaluators

| Command | Status | What it does |
|---------|--------|--------------|
| **`bt2live convergence`** | shipped | Read a variant parameter sweep → measure dispersion, IS/OOS rank stability, and parameter-plateau structure → verdict: **CONVERGED / ITERATE / KILL**. |
| **`bt2live fill-gap`** | shipped | Re-run your backtest's resting orders under a queue proxy on top-of-book and trades → your fill rate vs the proxy's: **CONSISTENT / OVERSTATED**. |

Roadmap: [ROADMAP.md](ROADMAP.md) and the [milestones](https://github.com/martianmobile/backtest2live/milestones).

---

## Install

```bash
pip install backtest2live
```

The core has no runtime dependencies (Python 3.9+). `fill-gap` needs numpy and pandas:

```bash
pip install 'backtest2live[data]'
```

**Claude Code plugin.** This repo is also a plugin marketplace. From inside Claude Code:

```
/plugin marketplace add martianmobile/backtest2live
/plugin install backtest2live@martianmobile
```

Then ask *"are these variants converged?"* or run `/convergence results.csv --metric sharpe_oos`. The skill installs the CLI if it is missing.

---

## `fill-gap`: do your backtest's passive fills survive a queue?

A backtest that fills a resting order whenever price touches it counts fills a live order would never get: the order sits behind the queue already displayed at that price. `fill-gap` re-runs each resting order in your order log against top-of-book and trade prints, and grants a fill only when:

- the volume printed at exactly your price, after you joined, covers the queue ahead of you plus your own size, or
- a trade prints through your price, which means the level was cleared.

```bash
bt2live fill-gap examples/orders_btcusdt_2024-03-30.csv --venue binance-um --pair BTCUSDT
```

```
## Verdict: OVERSTATED

**Your backtest fills 95.2% of its resting orders; the queue proxy fills 66.2%.
30.4% of the counted fills (834 of 2,740) are not granted on this data.**
```

The report continues with fill rates by rule, median time to fill, misses by volatility tercile and by UTC hour, and P&L on missed fills if your log has a `pnl` column. `--json` gives the same result machine-readable. Exit code: `0` CONSISTENT, `1` OVERSTATED (more than `--tolerance`, default 10%, of your fills not granted), `3` error.

### Your order log

CSV, one row per order. Column names are matched case-insensitively:

| Role | Accepted names | Required |
|------|----------------|----------|
| time | `ts`, `timestamp`, `time`, `datetime`, `created_at` | yes (epoch s/ms/µs/ns or ISO-8601, UTC) |
| side | `side` (`buy`/`sell`) | yes |
| price | `price`, `px`, `limit_price` | yes |
| size | `size`, `qty`, `quantity`, `amount` | yes (base units) |
| backtest's fill | `filled` (true/false, 1/0) or `fill_qty` | no: without it, the touch rule is the baseline |
| end of life | `end_ts`, `cancel_ts`, `expire_ts` | no: default `--max-rest 60` seconds |
| P&L | `pnl` | no |
| id | `order_id` | no |

Orders marketable at arrival are taker orders and are skipped. `--latency-ms` delays every order's arrival at the book.

### Market data

- **`--venue binance-um --pair BTCUSDT`** downloads Binance's public USDT-M futures archive (`bookTicker` + `aggTrades`) for the days your orders cover, checks the published SHA-256, and caches it under `~/.cache/backtest2live` (`BT2LIVE_CACHE` overrides). About 100 MB per day for BTCUSDT. Binance stopped publishing `bookTicker` after **2024-03-30**, so this source covers orders up to that day.
- **`--book l1.csv --trades trades.csv`** uses your own recorded data, any venue and any date. Book: `ts, bid_px, bid_qty, ask_px, ask_qty` (every change of the best bid/ask). Trades: `ts, price, qty` plus `is_buyer_maker` (true when a seller hit the bid) or the taker's `side`.

### What the proxy cannot see

The touch rule is an upper bound: a resting order fills only if something trades at or through its price. The proxy is an estimate below it. Hidden size, your latency and your own impact make it too generous. Cancels ahead of you, and a queue position read from the size shown when the price reached you, make it too strict. How that nets out depends on venue, size and regime. Only your own live fills settle it; [Martian Mobile](https://martianmobile.com/fill-autopsy?utm_source=github&utm_medium=readme&utm_campaign=fill-gap) runs that check against live fills.

---

## `convergence`: has your variant sweep converged?

Given one row per variant (parameters + metrics), it measures:

- **Top-K dispersion** — are the best variants clustered, or is one a lucky outlier?
- **IS↔OOS rank stability** — does the in-sample ordering survive out-of-sample? (Spearman)
- **Parameter-space structure** — do winners sit on a plateau, or are they isolated points (overfit)? Is the best variant pinned to the edge of the swept range?
- **Sample sufficiency** — are any top variants below a trade-count floor?

…then returns a verdict:

| Verdict | Meaning |
|---------|---------|
| **CONVERGED** | Tight, OOS-stable, plateau region. Ship the representative variant. |
| **ITERATE** | Some criteria unmet (edge peak, weak stability, high dispersion). Suggested next sweep included. |
| **KILL** | No stable edge — IS doesn't survive OOS, or best ≈ median. Abandon or rethink. |

---

### Usage

```bash
bt2live convergence examples/results_converged.csv --metric sharpe_oos
bt2live convergence examples/results_converged.csv --json    # machine-readable verdict
python -m backtest2live convergence results.csv              # same, without the script shim
```

Exit code encodes the verdict: `0` CONVERGED · `1` ITERATE · `2` KILL · `3` error (bad input or usage), so a CI job can gate on it.

### Input format

A CSV with one row per variant:

```
variant_id, lookback, threshold, sharpe_is, sharpe_oos, n_trades
v001, 20, 0.5, 1.82, 1.61, 412
v002, 25, 0.5, 1.85, 1.58, 401
...
```

Column roles are inferred from names/types. IS/OOS pairs are detected by suffix (`_is`/`_oos`, `_in`/`_out`, `_train`/`_test`). For Parquet/SQLite, export to CSV first.

### Flags

| Flag | Default | Purpose |
|------|---------|---------|
| `--metric` | auto | Ranking metric column |
| `--top-k` | 5 | Top variants to assess |
| `--dispersion-threshold` | 0.05 | Max relative dispersion for CONVERGED |
| `--min-samples` | 200 | Sample floor per variant |
| `--rank-threshold` | 0.6 | Min IS/OOS Spearman for CONVERGED |
| `--lower-is-better` / `--higher-is-better` | auto | Metric direction override |
| `--id-column` / `--sample-column` | auto | Column-role overrides |
| `--save` | off | Also write `iteration_check_<timestamp>.md` in the current directory |
| `--json` | off | Print the verdict and numbers as JSON instead of Markdown |

---

### Example output

```
# Iteration Check | results_converged.csv | 2026-06-06

## Verdict: CONVERGED

**Top-5 cluster tightly (1.7% dispersion) on a parameter plateau, OOS-stable (ρ=0.99) — ship `v10`.**

## Convergence Analysis
| Metric             | Top-K Mean | Top-K Std | Dispersion | Threshold |
|--------------------|-----------|-----------|------------|-----------|
| sharpe_oos (rank)  | 1.58      | 0.03      | 1.7%       | < 5% ✓    |
| sharpe_is          | 1.82      | 0.02      | 1.3%       | < 5% ✓    |

IS→OOS rank stability (Spearman): 0.99 (threshold > 0.60 ✓)

## Parameter-Space Check
- lookback: top-K at [20, 25, 30] — contiguous
- threshold: top-K at [0.5, 0.6] — contiguous
[x] Plateau region — robust, safe to deploy
```

---

## Limitations

- **Not a backtester.** It analyzes results you already have; it does not run strategies.
- **Not a live monitor.** Research-phase tooling — live trading needs different rails.
- **Metric-agnostic, not metric-smart.** It checks dispersion of whatever metric you point at; it doesn't know whether that metric is the right one.
- **Convergence ≠ profit.** Convergent variants can converge on losing strategies. The verdict reports convergence, not edge.

---

## Why this exists

In real quant research, the failure mode isn't running too few backtests — it's calling one lucky variant a "winner" when its neighbors perform completely differently. Convergence-across-variants is the only honest signal that the parameter region has structure. This tool enforces the discipline — and gives the same treatment to the other ways a strategy can look better than it is.

---

Built by [Martian Mobile](https://martianmobile.com). MIT licensed — see [LICENSE](LICENSE).
