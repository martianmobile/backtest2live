# backtest2live

[![CI](https://github.com/martianmobile/backtest2live/actions/workflows/ci.yml/badge.svg)](https://github.com/martianmobile/backtest2live/actions/workflows/ci.yml)

**Check a backtest against what live execution would do.** Evaluators run locally on your own files and public data, and each one returns a verdict with the numbers behind it.

Use it three ways: as a Python CLI (`bt2live`), as a library, or as a Claude Code plugin where the agent runs the CLI and interprets the verdict.

This is the public, MIT-licensed version of checks used in live crypto trading research at [Martian Mobile](https://martianmobile.com).

---

## Why

A backtest usually flatters a strategy in three places:

- **Resting orders.** Most engines fill a limit order the moment price touches it, as if it were first in line. On ten days of public BTCUSDT best-bid/ask and trade data (March 2024), that rule grants three fills for every two a queue-aware rule allows (40.2% vs 27.2% of joins at the best price). The extra fills are not random: they are the touches that bounced without clearing the queue, and in a toy strategy in the article linked below they lose far less per fill than the fills a queue allows, so the backtest overstates edge as well as fill rate.
- **Taker orders.** Bar-based backtests book market orders at the bar's price or the mid. A live order pays the spread, walks past the touch when it is larger than the displayed size, and arrives late.
- **Parameter sweeps.** The best variant of a sweep is often the luckiest one. Without a stability check it looks like an edge.

Each evaluator below checks one of these on your own files: `fill-gap` re-runs your resting orders against public top-of-book and trade data, `cost-gap` re-prices your taker orders against top-of-book at arrival, and `convergence` reads your sweep results. The full method, data, tables and a script to reproduce the resting-order result: [Fill at Touch Is a Backtest Lie](https://martianmobile.com/insights/article-4-fill-at-touch?utm_source=github&utm_medium=readme&utm_campaign=fill-gap).

---

## Evaluators

| Command | Status | What it does |
|---------|--------|--------------|
| **`bt2live convergence`** | shipped | Read a variant parameter sweep → measure dispersion, IS/OOS rank stability, and parameter-plateau structure → verdict: **CONVERGED / ITERATE / KILL**. |
| **`bt2live fill-gap`** | shipped | Re-run your backtest's resting orders under a queue proxy on top-of-book and trades → your fill rate vs the proxy's: **CONSISTENT / OVERSTATED**. |
| **`bt2live cost-gap`** | shipped | Re-price your backtest's taker orders (market, or limits that cross) on top-of-book at arrival, with latency → what they would have paid vs what the backtest booked: **CONSISTENT / OVERSTATED**. The check for bar-based backtests. |
| **`bt2live import`** | new | Convert an engine's export (NautilusTrader, LEAN, backtrader, vectorbt, freqtrade, Hummingbot) into the order log the checks read. |
| **`bt2live tearsheet`** | new | One shareable performance sheet (Markdown, HTML, JSON) whose header says where the numbers came from: **backtest** or **self-reported live**. |
| **`bt2live preflight`** | new | Every check above in one report: out-of-sample, execution realism, capacity, risk, each **pass / warn / fail / not run**. |

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

**Any MCP client (Claude Desktop, Cursor, others).** `bt2live mcp` serves the checks as MCP tools over stdio: `convergence`, `fill_gap`, `cost_gap`. Each returns the report and its `--json` result. It reads files on your machine and sends nothing to us; the only network calls are the public market-data downloads `fill_gap` and `cost_gap` make when you name a `venue`.

Claude Desktop (`claude_desktop_config.json`) or Cursor (`~/.cursor/mcp.json`, or `.cursor/mcp.json` in a project):

```json
{
  "mcpServers": {
    "backtest2live": {
      "command": "uvx",
      "args": ["--from", "backtest2live[data]", "bt2live", "mcp"]
    }
  }
}
```

With the package already installed, use `"command": "bt2live", "args": ["mcp"]`. Any other client that launches a stdio server works the same way. Pass absolute paths to your files in the tool arguments.

---

## `import`: your engine's export, as the order log the checks read

`fill-gap` and `cost-gap` read one order-log shape, whatever engine wrote the backtest. `bt2live import` writes it from each engine's own export:

```bash
bt2live import nautilus orders_report.csv -o orders.csv
bt2live import freqtrade backtest-result.json -o orders.csv --split   # one file per pair
```

| Engine | Pass this file | Feeds |
|---|---|---|
| `nautilus` | `trader.generate_orders_report().to_csv(...)` | fill-gap (limit orders, with fill flag and end time), cost-gap (market orders) |
| `lean` | the backtest's `<algorithm>-order-events.json` | fill-gap, cost-gap |
| `freqtrade` | a backtest result `.json` (unzipped), or an `orders` table CSV from a dry-run or live database | cost-gap; fill-gap from the orders CSV (canceled orders included) |
| `hummingbot` | `data/trades_<strategy>.csv` | cost-gap (fills only) |
| `vectorbt` | `pf.orders.records_readable.to_csv(...)` | cost-gap (fills at the given price) |
| `backtrader` | the `Transactions` analyzer, `get_analysis()` dumped to JSON, or as a CSV | cost-gap (fills only) |

A trade list holds fills only: every row is filled, so it feeds cost-gap and has nothing for fill-gap to re-run. The freqtrade orders export has no pair column; the pair is parsed from the dry-run order id. In dry-run, freqtrade's `filled` comes from its own simulator, so fill-gap compares that simulator with the queue proxy. `--symbol` keeps one instrument; `--split` writes one file per instrument, because the checks run one `--pair` at a time.

**Order-log schema** (`backtest2live.orders/1`), one row per order:

| Column | Meaning |
|---|---|
| `order_id`, `symbol` | the engine's id and instrument name |
| `ts` | arrival time, epoch ms UTC |
| `side`, `type` | `buy`/`sell`, `market`/`limit` |
| `price` | the limit (blank for market orders) |
| `size`, `fill_qty` | order size and the size the backtest filled, base units |
| `filled` | 1 if the backtest filled any of it |
| `fill_price` | average price the backtest booked |
| `end_ts` | end of the order's life (fill, cancel, expiry), epoch ms |
| `pnl` | P&L the backtest booked on the order, when the engine reports it |

fill-gap skips the market rows and cost-gap the resting ones, so one file serves both. Any CSV with these columns works, whatever wrote it.

**Results schema** (what `convergence` reads): one row per variant, an id column (`variant_id`), one column per swept parameter, metric columns with `_is`/`_oos` suffixes for in-sample and out-of-sample (`sharpe_is`, `sharpe_oos`), and a sample count (`n_trades`). See [`convergence`](#convergence-has-your-variant-sweep-converged) below.

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

Orders marketable at arrival are taker orders: `fill-gap` skips them and `cost-gap` prices them. `--latency-ms` delays every order's arrival at the book.

### Market data

| Source | Flag | Covers | Size |
|---|---|---|---|
| Binance USDT-M futures public archive | `--venue binance-um --pair BTCUSDT` | `bookTicker` + `aggTrades`, up to **2024-03-30** (Binance stopped publishing top-of-book after that) | ~100 MB/day |
| Bybit perpetuals public archive | `--venue bybit-linear --pair BTCUSDT` (or `bybit-inverse`) | L2 order book (snapshot + deltas, replayed to top-of-book) + trades, **2023 to yesterday** | 75–215 MB/day book, ~50 MB/day trades |
| Your own L2 day file | `--l2 FILE [FILE …]` | an unpacked OKX historical-data portal `books` file (newline JSON, snapshot then updates), a Bybit `ob*.data(.zip)` file, or a sampled-snapshot CSV `timestamp,bids,asks` with JSON level lists; any date | — |
| Your own top-of-book | `--book l1.csv` | `ts, bid_px, bid_qty, ask_px, ask_qty`, every change of the best bid/ask; any venue, any date | — |

Downloads are cached under `~/.cache/backtest2live` (`BT2LIVE_CACHE` overrides); Binance files are checked against the published SHA-256. A downloaded day's L2 replay is cached next to it, so it is replayed once (about 10 s for a Bybit BTCUSDT day); files passed with `--l2` are replayed on each run. `fill-gap` also needs trades: `--venue` fetches them; with `--book`/`--l2` pass `--trades trades.csv` (`ts, price, qty` plus `is_buyer_maker`, true when a seller hit the bid, or the taker's `side`; OKX and Bybit trade exports work as is). If your network blocks the archive hosts, download the same Bybit files by hand and pass them as they are: the `ob*.data.zip` files with `--l2`, the `.csv.gz` trades with `--trades`. The tick is read from the market data, per day with `--venue`, so a venue's tick change inside the window is followed; `--tick` overrides it.

### What the proxy cannot see

The touch rule is an upper bound: a resting order fills only if something trades at or through its price. The proxy is an estimate below it. Hidden size, your latency and your own impact make it too generous. Cancels ahead of you, and a queue position read from the size shown when the price reached you, make it too strict. How that nets out depends on venue, size and regime. Only your own live fills settle it; [Martian Mobile](https://martianmobile.com/fill-autopsy?utm_source=github&utm_medium=readme&utm_campaign=fill-gap) runs that check against live fills.

---

## `cost-gap`: what would your taker orders have paid?

A bar-based backtest fills every order at a bar price: the close, the open, or the mid. A real market order crosses the spread, eats whatever is displayed at the touch, and arrives some milliseconds after the book you saw. `cost-gap` re-prices each market order, and each limit that crosses the spread, against top-of-book at its arrival time and reports the cost against the price the backtest booked.

```bash
bt2live cost-gap examples/market_orders_btcusdt_2024-03-30.csv --venue binance-um --pair BTCUSDT
```

```
## Verdict: REPORTED

**Your backtest's 1,435 taker orders would have paid +0.01 bps on average
(+0.01 bps median) against the price it booked.**
```

That example is a toy momentum strategy sending 0.1 BTC market orders every minute and booking the mid. On BTCUSDT perpetuals the half-spread is under 0.01 bps and the touch absorbs 0.1 BTC, so the mid-fill assumption holds there, and the report says so. The check bites on thinner pairs, larger sizes and slower paths: the latency table (0 / 5 / 20 / 100 ms) shows the slope, and the by-volatility table shows where it concentrates. With a `pnl` column the verdict is **OVERSTATED** when the cost removes more than `--tolerance` (default 25%) of the backtest's P&L; without one it is **REPORTED**. Exit code: `0` CONSISTENT or REPORTED, `1` OVERSTATED, `3` error.

### Your order log

Same columns as `fill-gap`, with three additions: `type` (`market`/`limit`), `price` for limits (a limit that crosses the touch is priced as a taker and capped at its limit; one that rests is counted and left to `fill-gap`), and `fill_price`, the price the backtest booked (without it, the mid at arrival is the baseline). A log with no `price` column is all market orders. A log with a `price` column but no `type` and no `fill_price` is read as a bar backtest's export: the price is the booked fill, every order is a market order, and the run says so.

### What the book can and cannot see

Top-of-book shows the touch and its displayed size, nothing behind it. Size beyond the touch is priced one tick worse per displayed-size slice, the least impact consistent with the feed, so the cost is a floor, not a measurement. Only your own live fills settle it.

---

## `tearsheet`: one sheet, labelled with where its numbers came from

Tear sheets are commodity; what they rarely say is where the numbers came from. Every `bt2live tearsheet` prints its provenance in the header, and the flag is required:

```bash
bt2live tearsheet equity.csv --provenance backtest --assumption "limit fills at touch" --check fill-gap.json --out sheet
bt2live tearsheet my_bybit_history.csv --provenance self-reported --venue Bybit --capital 10000
```

| `--provenance` | Header |
|---|---|
| `backtest` | Backtest — simulated fills, assumptions listed below |
| `self-reported` | Live, self-reported — read from `<venue>` trade history; account and period chosen by the user |

A third level, a live pilot recorded by Martian Mobile on its own infrastructure, is generated there with a verification link; this tool never writes it.

Input is an equity curve (`ts`, `equity`) or an order log with a `pnl` column plus `--capital` (equity is then capital plus realized P&L; open positions are not marked). The sheet has total return, CAGR, annualized volatility and Sharpe (from daily closes, shown from 30 days of returns), max drawdown and the longest time under water, MAR, the worst day / 7 / 30 days, and monthly returns; from an order log also profit factor, turnover, fees (with a `fee` column) and time in market. `--assumption` lists the fill model and costs, and `--check` cites a `bt2live … --json` result (fill-gap, cost-gap, convergence) with its verdict. `--out sheet` writes `sheet.html` (print it to PDF from the browser) and `sheet.json`; the footer carries the SHA-256 of the JSON without its hash field, so a reader can check the numbers were not edited. Nothing leaves your machine.

---

## `preflight`: every check in one report

```bash
bt2live preflight --results sweep.csv --orders orders.csv --venue bybit-linear --pair BTCUSDT --out preflight
```

Runs `convergence` on `--results` and `fill-gap` + `cost-gap` on `--orders` (same market-data flags), then writes one report in four sections, the shape of a live desk's review gate: **out-of-sample**, **execution realism**, **capacity**, **risk**. Each is marked pass / warn / fail / not run, says why a check did not run, and states what a local check on public data cannot see. Capacity and risk have no check yet and always read *not run*. A log without a `type` column holds one kind of order, so only one of fill-gap (it has a fill flag or end time) and cost-gap runs on it; add `type` (market/limit) to run both. `--out preflight` writes `preflight.md` and `preflight.json`. Exit code: `0` every check that ran passed, `1` a warning (ITERATE, or cost-gap without P&L), `2` a failure, `3` error or no check ran.

---

## `convergence`: has your variant sweep converged?

Given one row per variant (parameters + metrics), it measures:

- **Top-K dispersion** — are the best variants clustered, or is one a lucky outlier?
- **IS↔OOS rank stability** — does the in-sample ordering survive out-of-sample? (Spearman)
- **Parameter-space structure** — do winners sit on a plateau, or are they isolated points (overfit)? Is the best variant pinned to the edge of the swept range?
- **Sample sufficiency** — are any top variants below a trade-count floor?
- **Selection bias** — when the metric is a Sharpe and the return count is known: the **deflated Sharpe** (Bailey & López de Prado), the probability the winner beats the best Sharpe that the same number of skill-less trials would show. With fold columns (`sharpe_is_1`, `sharpe_oos_1`, `sharpe_is_2`, …) also the **probability of backtest overfitting**: how often each fold's in-sample winner lands at or below the out-of-sample median. A miss turns CONVERGED into ITERATE, never KILL: at 0.95 the deflated Sharpe is strict on short samples. Correlated variants are fewer independent trials than rows; pass `--trials` with an effective count.

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
| `--n-obs` | column | Returns behind each Sharpe, for the deflated Sharpe (else a `n_obs` / `n_days` / `n_periods` column) |
| `--trials` | variants | Effective number of independent trials for the deflated Sharpe |
| `--periods-per-year` | 365 | How the Sharpe is annualized (252 for equities, 1 if per period) |
| `--dsr-threshold` / `--pbo-threshold` | 0.95 / 0.5 | Selection-bias bars for CONVERGED |
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
