# Iteration Check | results_drawdown.csv | 2026-06-06

## Verdict: ITERATE

**Convergence not yet achieved — some criteria unmet (see below).**

---

## Convergence Analysis

Ranking metric: `max_drawdown` (lower is better) · Top-3 of 6 variants

| Metric | Top-K Mean | Top-K Std | Dispersion | Threshold |
|--------|-----------|-----------|------------|-----------|
| max_drawdown *(rank)* | 0.07 | 0.00 | 6.7% | < 5% ✗ |

**Read:** Top-3 are spread out on the ranking metric (6.7%).

---

## Parameter-Space Check

- `window`: top-K at [20, 30, 40] — contiguous

[ ] Single isolated peak — fragile, retest with more samples
[x] Plateau region — robust, safe to deploy

Sample sizes OK — all top-3 variants ≥ 200 on `n_trades`.

---

## Recommendation

**[ITERATE]** Convergence not yet achieved. Suggested next sweep:
- Tighten dispersion: top-K spread is 6.7% (> 5%) — narrow the grid or add samples

