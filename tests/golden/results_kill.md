# Iteration Check | results_kill.csv | 2026-06-06

## Verdict: KILL

**IS/OOS rank correlation -1.00 ≤ 0 — in-sample ordering does not survive out-of-sample**

---

## Convergence Analysis

Ranking metric: `sharpe_oos` (higher is better) · Top-5 of 10 variants

| Metric | Top-K Mean | Top-K Std | Dispersion | Threshold |
|--------|-----------|-----------|------------|-----------|
| sharpe_oos *(rank)* | 0.85 | 0.14 | 16.1% | < 5% ✗ |
| sharpe_is | 1.34 | 0.12 | 8.9% | < 5% ✗ |

**IS→OOS rank stability (Spearman):** -1.00 (threshold > 0.60 ✗)

**Read:** Top-5 are spread out on the ranking metric (16.1%); is↔oos rank correlation is -1.00.

---

## Parameter-Space Check

- `lookback`: top-K at [20, 25, 30] **(edge of swept range)** — contiguous
- `threshold`: top-K at [0.4, 0.6] **(edge of swept range)** — contiguous

[ ] Single isolated peak — fragile, retest with more samples
[x] Plateau region — robust, safe to deploy

Sample sizes OK — all top-5 variants ≥ 200 on `n_trades`.

---

## Recommendation

**[KILL]** No region of the swept parameter space shows a stable edge. Specifically:
- IS/OOS rank correlation -1.00 ≤ 0 — in-sample ordering does not survive out-of-sample
Recommend abandoning this strategy line, or rethinking the metric/feature set before re-sweeping.

