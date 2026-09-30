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


def test_crash_is_error_not_verdict(tmp_path, capsys):
    bad = tmp_path / "latin1.csv"
    bad.write_bytes(b"variant_id,sharpe\nv1,1.0\nv\xe9,2.0\n")
    assert cli.main(["convergence", str(bad)]) == cli.EXIT_ERROR
    assert "error:" in capsys.readouterr().err
    assert cli.main(["convergence", str(bad), "--top-k", "0"]) == cli.EXIT_ERROR


def test_json_never_emits_non_finite(write_csv, capsys):
    p = write_csv("variant_id,lookback,sharpe\nv1,inf,1.0\nv2,20,2.0\nv3,nan,0.5\nv4,30,1.5")
    rc = cli.main(["convergence", p, "--metric", "sharpe", "--json"])
    out = capsys.readouterr().out
    assert "Infinity" not in out and "NaN" not in out
    # inf/nan cells are garbage, and a column with garbage is not numeric
    assert rc == 2 and "lookback" not in json.loads(out)["parameters"]


def test_plugin_examples_match_repo_examples(root):
    for name in ("results_converged.csv", "results_iterate.csv"):
        a = (root / "examples" / name).read_bytes()
        b = (root / "plugins/backtest2live/skills/convergence/examples" / name).read_bytes()
        assert a == b, name
