import json
import subprocess
import sys

import pytest

from backtest2live import cli

GOLDEN = [
    # (input, extra args, golden, exit code)
    ("examples/results_converged.csv", ["--metric", "sharpe_oos"], "results_converged.md", 0),
    ("examples/results_iterate.csv", ["--metric", "sharpe_oos"], "results_iterate.md", 1),
    ("tests/fixtures/results_kill.csv", ["--metric", "sharpe_oos"], "results_kill.md", 2),
    ("tests/fixtures/results_drawdown.csv", ["--metric", "max_drawdown", "--top-k", "3"],
     "results_drawdown.md", 1),
]


@pytest.mark.parametrize("path,extra,golden,code", GOLDEN)
def test_report_matches_pre_package_analyzer(root, capsys, path, extra, golden, code):
    # Goldens were produced by plugins/strategy-evaluation/.../analyze.py before the move.
    rc = cli.main(["convergence", str(root / path), "--date", "2026-06-06", *extra])
    out = capsys.readouterr().out
    assert rc == code
    assert out == (root / "tests" / "golden" / golden).read_text(encoding="utf-8")


def test_json_carries_verdict_and_numbers(root, capsys):
    rc = cli.main(["convergence", str(root / "examples/results_converged.csv"),
                   "--metric", "sharpe_oos", "--json"])
    doc = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert doc["schema"] == "backtest2live.convergence/1"
    assert doc["verdict"] == "CONVERGED" and doc["exit_code"] == 0
    assert doc["metric"] == "sharpe_oos" and doc["is_column"] == "sharpe_is"
    assert doc["is_oos_spearman"] > 0.6
    assert doc["dispersion"]["rank"] < 0.05
    assert set(doc["parameters"]) == {"lookback", "threshold"}


def test_missing_file_is_error_not_verdict(capsys):
    assert cli.main(["convergence", "/nonexistent/results.csv"]) == cli.EXIT_ERROR
    assert "file not found" in capsys.readouterr().err


def test_usage_error_is_error_not_kill():
    with pytest.raises(SystemExit) as e:
        cli.main(["convergence"])
    assert e.value.code == cli.EXIT_ERROR


def test_save_writes_report(root, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cli.main(["convergence", str(root / "examples/results_converged.csv"), "--save"])
    saved = list(tmp_path.glob("iteration_check_*.md"))
    assert len(saved) == 1 and "## Verdict: CONVERGED" in saved[0].read_text(encoding="utf-8")


def test_module_entry_point(root):
    r = subprocess.run([sys.executable, "-m", "backtest2live", "convergence",
                        str(root / "examples/results_iterate.csv"), "--json"],
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert json.loads(r.stdout)["verdict"] == "ITERATE"


def test_core_is_stdlib_only(root):
    # Block the heavy deps, then import the CLI and run it: the core must not need them.
    code = (
        "import sys\n"
        "for m in ('pandas', 'numpy', 'scipy'): sys.modules[m] = None\n"
        "from backtest2live import cli\n"
        f"sys.exit(cli.main(['convergence', {str(root / 'examples/results_converged.csv')!r}, '--json']))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_fill_gap_without_numpy_says_how_to_install(root, tmp_path):
    orders = tmp_path / "o.csv"
    orders.write_text("ts,side,price,size\n1,buy,1,1\n")
    code = (
        "import sys\n"
        "for m in ('pandas', 'numpy'): sys.modules[m] = None\n"
        "from backtest2live import cli\n"
        f"sys.exit(cli.main(['fill-gap', {str(orders)!r}, '--venue', 'binance-um', '--pair', 'X']))\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 3
    assert "backtest2live[data]" in r.stderr
