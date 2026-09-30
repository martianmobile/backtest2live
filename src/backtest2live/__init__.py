"""backtest2live — check a backtest against what live execution would do.

Evaluators run locally on your files and public data. Each is a `bt2live`
subcommand and an importable module.
"""

try:
    from importlib.metadata import PackageNotFoundError, version

    __version__ = version("backtest2live")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"
