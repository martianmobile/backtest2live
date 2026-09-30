# Iteration Check | results_iterate.csv | 2026-06-06

## Verdict: ITERATE

**Convergence not yet achieved — some criteria unmet (see below).**

---

## Convergence Analysis

Ranking metric: `sharpe_oos` (higher is better) · Top-5 of 16 variants

| Metric | Top-K Mean | Top-K Std | Dispersion | Threshold |
|--------|-----------|-----------|------------|-----------|
| sharpe_oos *(rank)* | 1.63 | 0.11 | 6.6% | < 5% ✗ |
| sharpe_is | 1.85 | 0.10 | 5.6% | < 5% ✗ |

**IS→OOS rank stability (Spearman):** 1.00 (threshold > 0.60 ✓)

**Read:** Top-5 are spread out on the ranking metric (6.6%); is↔oos rank correlation is 1.00.

---

## Parameter-Space Check

- `lookback`: top-K at [25, 30] **(edge of swept range)** — contiguous
- `threshold`: top-K at [0.4, 0.5, 0.6] — contiguous

[ ] Single isolated peak — fragile, retest with more samples
[x] Plateau region — robust, safe to deploy

Sample sizes OK — all top-5 variants ≥ 200 on `n_trades`.

---

## Recommendation

**[ITERATE]** Convergence not yet achieved. Suggested next sweep:
- Expand `lookback` beyond its current edge (top variant sits at 30, the boundary of the swept grid)
- Tighten dispersion: top-K spread is 6.6% (> 5%) — narrow the grid or add samples

