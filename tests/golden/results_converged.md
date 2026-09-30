# Iteration Check | results_converged.csv | 2026-06-06

## Verdict: CONVERGED

**Top-5 cluster tightly (1.7% dispersion) on a parameter plateau, OOS-stable (ρ=0.99) — ship `v10`.**

---

## Convergence Analysis

Ranking metric: `sharpe_oos` (higher is better) · Top-5 of 16 variants

| Metric | Top-K Mean | Top-K Std | Dispersion | Threshold |
|--------|-----------|-----------|------------|-----------|
| sharpe_oos *(rank)* | 1.58 | 0.03 | 1.7% | < 5% ✓ |
| sharpe_is | 1.82 | 0.02 | 1.3% | < 5% ✓ |

**IS→OOS rank stability (Spearman):** 0.99 (threshold > 0.60 ✓)

**Read:** Top-5 are clustered tightly on the ranking metric (1.7%); is↔oos rank correlation is 0.99.

---

## Parameter-Space Check

- `lookback`: top-K at [20, 25, 30] — contiguous
- `threshold`: top-K at [0.5, 0.6] — contiguous

[ ] Single isolated peak — fragile, retest with more samples
[x] Plateau region — robust, safe to deploy

Sample sizes OK — all top-5 variants ≥ 200 on `n_trades`.

---

## Recommendation

**[CONVERGED]** Top variant `v10` is representative — its neighbors perform similarly and the winning region is a plateau, not a lucky point. Safe to advance this variant to the next stage.

