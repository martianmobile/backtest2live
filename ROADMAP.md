# Roadmap

`backtest2live` checks a backtest against what live execution would do. It grows by adding evaluators: each is a `bt2live` subcommand, an importable module, and a Claude Code skill.

Tracking: [milestones](https://github.com/martianmobile/backtest2live/milestones). Order within a milestone follows the `priority:` labels.

## v0.2 — first PyPI release (early October 2026)

- **Package + CLI** — [#5](https://github.com/martianmobile/backtest2live/issues/5). `pip install backtest2live`, `bt2live <evaluator>`, `--json` on every command; the plugin wraps the CLI.
- **`fill-gap`** — [#6](https://github.com/martianmobile/backtest2live/issues/6). Your backtest's resting orders re-run under a queue proxy on public order-book data: naive vs proxy fill rate, time to fill, misses by hour and volatility.
- **Tests + CI** — [#3](https://github.com/martianmobile/backtest2live/issues/3). pytest on Python 3.9–3.13; the core stays dependency-free.
- **Release** — [#11](https://github.com/martianmobile/backtest2live/issues/11). Closing a milestone publishes the version to GitHub Releases and PyPI.

## v0.3 — run the contract, check any engine, share the output (target mid-October 2026)

Two ways in. A strategy written to the published `StrategyProtocol` contract (the format Validate accepts) runs locally with `bt2live run`. A backtest from any other engine connects through the order-log schema and its adapters.

- **`bt2live run`** — [#15](https://github.com/martianmobile/backtest2live/issues/15). Load a strategy package on the published contract, build its inputs from public data, simulate fills at both ends of the band (front of queue vs the `fill-gap` queue proxy) and taker fills through the published simulator. Emits order logs in the `fill-gap` schema and two equity curves.
- **`cost-gap`** — [#16](https://github.com/martianmobile/backtest2live/issues/16). For bar-based backtests, which have no resting orders: each market order re-priced against the real book at its time, with latency. Spread, depth and latency cost vs the backtest's fill price.
- **Schema + engine adapters** — [#7](https://github.com/martianmobile/backtest2live/issues/7). One order-log and results schema; importers for Nautilus, LEAN, backtrader, vectorbt, freqtrade, so the export is one command.
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
