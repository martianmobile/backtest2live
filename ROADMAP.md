# Roadmap

`backtest2live` checks a backtest against what live execution would do. It grows by adding evaluators: each is a `bt2live` subcommand, an importable module, and a Claude Code skill.

Tracking: [milestones](https://github.com/martianmobile/backtest2live/milestones). Order within a milestone follows the `priority:` labels.

## v0.2 — first PyPI release

- **Package + CLI** — [#5](https://github.com/martianmobile/backtest2live/issues/5). `pip install backtest2live`, `bt2live <evaluator>`, `--json` on every command; the plugin wraps the CLI.
- **`fill-gap`** — [#6](https://github.com/martianmobile/backtest2live/issues/6). Your backtest's resting orders re-run under a queue proxy on public order-book data: naive vs proxy fill rate, time to fill, misses by hour and volatility.
- **Tests + CI** — [#3](https://github.com/martianmobile/backtest2live/issues/3). pytest on Python 3.9–3.13; the core stays dependency-free.
- **Release** — [#11](https://github.com/martianmobile/backtest2live/issues/11). Closing a milestone publishes the version to GitHub Releases and PyPI.

## v0.3 — any engine, shareable output

- **Schema + engine adapters** — [#7](https://github.com/martianmobile/backtest2live/issues/7). One order-log and results schema; importers for Nautilus, LEAN, backtrader, vectorbt, freqtrade.
- **Tear sheet** — [#10](https://github.com/martianmobile/backtest2live/issues/10). One shareable performance artifact with a provenance label.
- **MCP server** — [#8](https://github.com/martianmobile/backtest2live/issues/8). The checks, exposed to any AI assistant.
- **Deflated Sharpe + PBO** — [#2](https://github.com/martianmobile/backtest2live/issues/2). Is the winner real, or did N variants overfit?
- **Preflight report** — [#9](https://github.com/martianmobile/backtest2live/issues/9). The checks in one document.
- **Cardinality-based column inference** — [#1](https://github.com/martianmobile/backtest2live/issues/1). Classify parameters vs metrics by cardinality, not keywords (today `window` reads as a metric because it contains `win`).

## Later (directional)

- **`robustness`** — outlier-trade drop test, bootstrap CIs on Sharpe, parameter-perturbation sensitivity.
- **`walk-forward`** — multi-fold rolling/anchored stability of a chosen variant.
- **`regime`** — performance by vol/trend regime; flags strategies that only work in one.
- **Visual report** — parameter-grid heatmap with the plateau and edge highlighted.

---

Contributions welcome. Built by [Martian Mobile](https://martianmobile.com).
