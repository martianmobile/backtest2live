---
name: convergence
description: Evaluate whether a variant backtest sweep has converged — read the results, measure top-K dispersion, IS/OOS rank stability, and parameter-plateau structure, then decide ship vs iterate vs kill. For quant researchers running parameter sweeps. Trigger with "check convergence", "are these variants converged?", "should I keep iterating?", "evaluate this sweep", "iteration check", "convergence report".
---

# Convergence — variant-sweep evaluation

The first capability of the **backtest2live** plugin. Drop in a table of variant backtest results — get a convergence verdict and a recommendation: ship the winner, iterate further, or kill the line of research.

This is the public, sanitized version of an internal pattern used to run multi-variant parameter sweeps in live crypto trading research at Martian Mobile. The convergence math is computed deterministically by the `backtest2live` Python package (`bt2live` CLI); the qualitative call on *which variants to try next* is left to the agent.

> Sibling evaluators (robustness, walk-forward, regime breakdown) can live alongside this one as additional skills under the same plugin.

## How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│             STRATEGY EVALUATION · CONVERGENCE                    │
├─────────────────────────────────────────────────────────────────┤
│  INPUT                                                           │
│  ✓ Variant results: CSV (Parquet/SQLite → export to CSV first)  │
│  ✓ Each row = one variant (params + metrics)                    │
│  ✓ You specify the convergence metric (Sharpe, hit-rate, etc.)  │
├─────────────────────────────────────────────────────────────────┤
│  ANALYSIS  (bt2live convergence, stdlib only)                   │
│  ✓ Dispersion of top-K variants on chosen metric                │
│  ✓ Stability across IS / OOS windows (Spearman, if present)     │
│  ✓ Parameter-space neighborhood check (winners cluster?)        │
├─────────────────────────────────────────────────────────────────┤
│  OUTPUT                                                          │
│  ✓ Verdict: CONVERGED / ITERATE / KILL                          │
│  ✓ Reasoning with the numbers behind it                         │
│  ✓ If ITERATE: suggested next variants to try                   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Getting Started

Point me at your variant results. Tell me the metric you care about. I'll do the rest.

```
You: /convergence results.csv --metric sharpe_oos
Me:  [Runs the analyzer, interprets the verdict, returns the report]
```

**Required columns in your input file:**
- A variant identifier (column name flexible: `variant_id`, `run_id`, `params`, etc.)
- One or more performance metrics (Sharpe, PnL, win-rate, drawdown, custom)

**Optional but useful:**
- Parameter columns (lets me check whether winners cluster in parameter space)
- In-sample / out-of-sample pairs (e.g. `sharpe_is` + `sharpe_oos`) for rank-stability
- A sample-size column (`n_trades`, etc.) — variants with too few trades get flagged

---

## Input Format (example)

A CSV with one row per variant works fine:

```
variant_id, lookback, threshold, sharpe_is, sharpe_oos, n_trades
v001, 20, 0.5, 1.82, 1.61, 412
v002, 20, 0.6, 1.79, 1.55, 388
v003, 25, 0.5, 1.85, 1.58, 401
...
```

The analyzer infers what's a metric and what's a parameter from column names and types. IS/OOS pairs are detected by suffix (`_is`/`_oos`, `_in`/`_out`, `_train`/`_test`). Override any inference with `--metric`, `--id-column`, `--sample-column`.

Runnable samples ship beside this skill:
- `${CLAUDE_PLUGIN_ROOT}/skills/convergence/examples/results_converged.csv` → CONVERGED
- `${CLAUDE_PLUGIN_ROOT}/skills/convergence/examples/results_iterate.csv` → ITERATE

---

## Execution Flow

When invoked, do this:

### Step 1 — Locate the input
Resolve the file path the user gave (a CSV, or a Parquet/SQLite they should export to CSV). If they didn't name a metric, you can let the analyzer auto-detect, but prefer to confirm the ranking metric if it's ambiguous.

### Step 2 — Run the analyzer (it does the math)
Run the `bt2live` CLI. If it is not installed, run it through an isolated runner from PyPI instead of installing into the user's Python:

```bash
SPEC="backtest2live"
ARGS=(convergence <input.csv> --metric <metric>)   # plus [--top-k 5] [--save] [--json]
if   command -v bt2live >/dev/null; then bt2live "${ARGS[@]}"
elif command -v uvx     >/dev/null; then uvx --from "$SPEC" bt2live "${ARGS[@]}"
elif command -v pipx    >/dev/null; then pipx run --spec "$SPEC" bt2live "${ARGS[@]}"
else echo "no runner: ask before installing"; fi
```

If none of the three exists, ask before installing anything (`python3 -m venv ~/.bt2live && ~/.bt2live/bin/pip install "$SPEC"` works everywhere; plain `pip install --user` is refused on many systems). Never install a package from any other source than `$SPEC`.

Use `--json` when you need the numbers programmatically (same verdict, machine-readable).

Useful flags (defaults match the config below):
- `--metric` ranking metric column (auto-detected if omitted)
- `--top-k` number of top variants to assess (default 5)
- `--dispersion-threshold` max relative dispersion for CONVERGED (default 0.05)
- `--min-samples` min samples/variant before a variant is trusted (default 200)
- `--rank-threshold` min IS/OOS Spearman for CONVERGED (default 0.6)
- `--lower-is-better` / `--higher-is-better` direction override
- `--save` also writes `iteration_check_<timestamp>.md`

The CLI prints the full Markdown report and sets an exit code: `0` CONVERGED, `1` ITERATE, `2` KILL, `3` error (bad input or usage).

### Step 3 — Relay and interpret
Present the analyzer's report. Then add value the deterministic analyzer cannot:
- **CONVERGED** → confirm the winning variant and what "advance to next stage" means for their pipeline.
- **ITERATE** → turn the analyzer's generic suggestions into *concrete next variants*: name the parameter ranges to expand, the new parameter to add (e.g. a vol filter if winners share a regime), or the resampling to run. Be specific.
- **KILL** → state plainly that no stable edge exists, and what would have to change (different metric, different feature set, more data) before re-sweeping is worth it.

Never override the analyzer's numbers — interpret them.

---

## Output Format

```markdown
# Iteration Check | [Run name] | [Date]

## Verdict: CONVERGED | ITERATE | KILL

**[One-line summary of why]**

---

## Convergence Analysis

| Metric | Top-K Mean | Top-K Std | Dispersion | Threshold |
|--------|-----------|-----------|------------|-----------|
| sharpe_oos (rank) | 1.58 | 0.03 | 1.7% | < 5% ✓ |
| sharpe_is         | 1.82 | 0.02 | 1.3% | < 5% ✓ |

**IS→OOS rank stability (Spearman):** 0.99 (threshold > 0.60 ✓)

**Read:** Top-K are clustered tightly on both IS and OOS — real convergence, not one lucky variant.

---

## Parameter-Space Check

- `lookback`: top-K at [20, 25, 30] — contiguous
- `threshold`: top-K at [0.5, 0.6] — contiguous
[ ] Single isolated peak — fragile, retest with more samples
[x] Plateau region — robust, safe to deploy

---

## Recommendation

[CONVERGED] Top variant `v10` is representative — neighbors perform similarly. Safe to advance.
```

---

## Verdict Logic

```
CONVERGED (all must hold):
- Top-K dispersion < threshold (default 5%)
- IS/OOS rank correlation > threshold (default 0.6, if OOS available)
- Winners cluster in parameter space (plateau, not isolated points)
- Top variant NOT at the edge of the swept range
- Minimum sample size per variant met (if a sample column exists)

ITERATE if:
- Some criteria met but not all
- Top variant near the edge of the swept parameter range
- IS/OOS gap or weak rank stability suggests undersampling

KILL if:
- IS ranking does not survive OOS (Spearman ≤ 0)
- Top variants are scattered with high dispersion — no stable region
- Best variant is statistically indistinguishable from the median
```

---

## Configuration

Defaults work for most cases. Override via CLI flags:

```yaml
metric: sharpe_oos
top_k: 5
dispersion_threshold: 0.05    # 5% relative
min_samples_per_variant: 200
rank_stability_threshold: 0.6
```

---

## Limitations (be honest)

- **Not a backtester.** Analyzes results you already have. It does not run strategies.
- **Not a live monitor.** Research-phase tooling. Live trading needs different rails.
- **Metric-agnostic, not metric-smart.** It checks dispersion of whatever metric you point at — it doesn't know whether your metric is the right one.
- **Convergence ≠ profit.** Convergent variants can converge on losing strategies. The verdict reports convergence, not edge.

---

## Why This Exists

In real quant research, the failure mode isn't running too few backtests — it's calling one lucky variant a "winner" when its neighbors have completely different performance. Convergence-across-variants is the only honest signal that the parameter region has structure. This capability enforces the discipline.

Built by [Martian Mobile](https://martianmobile.com). MIT-licensed, public sanitized version of an internal workflow.
