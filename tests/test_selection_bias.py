import json
import math
import statistics

import pytest

from backtest2live import cli, convergence


def run(capsys, path, *extra):
    rc = cli.main(["convergence", str(path), "--metric", "sharpe_oos", "--json", *extra])
    return rc, json.loads(capsys.readouterr().out)


def test_converged_looking_sweep_on_a_short_sample_fails_dsr(root, capsys):
    # the shipped CONVERGED example, over 180 daily returns: 16 trials, winner 1.61 annualized
    rc, d = run(capsys, root / "tests/fixtures/results_converged_180d.csv")
    assert d["verdict"] == "ITERATE" and rc == 1
    assert d["deflated_sharpe"]["passed"] is False and d["deflated_sharpe"]["dsr"] == pytest.approx(0.8103, abs=1e-4)
    assert "deflated Sharpe" in d["adjustments"][0]
    assert d["kill_reasons"] == []  # a DSR miss never kills


def test_the_same_sweep_on_a_long_sample_stays_converged(root, capsys):
    rc, d = run(capsys, root / "tests/fixtures/results_converged_2000d.csv")
    assert d["verdict"] == "CONVERGED" and rc == 0
    assert d["deflated_sharpe"]["dsr"] > 0.99 and d["adjustments"] == []
    assert set(d["parameters"]) == {"lookback", "threshold"}  # n_days is not a swept parameter


def test_dsr_matches_the_published_formula():
    srs = [0.05, 0.06, 0.08, 0.04, 0.07]
    sr, t, n = 0.08, 500, 5
    nd = statistics.NormalDist()
    g = 0.5772156649015329
    sr0 = statistics.stdev(srs) * ((1 - g) * nd.inv_cdf(1 - 1 / n) + g * nd.inv_cdf(1 - 1 / (n * math.e)))
    want = nd.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - (-0.5) * sr + (6 - 1) / 4 * sr ** 2))
    got, got_sr0 = convergence.deflated_sharpe(sr, srs, t, n, skew=-0.5, kurt=6)
    assert got == pytest.approx(want) and got_sr0 == pytest.approx(sr0)


def test_trials_override_lowers_the_hurdle(root, capsys):
    _, d16 = run(capsys, root / "tests/fixtures/results_converged_180d.csv")
    _, d4 = run(capsys, root / "tests/fixtures/results_converged_180d.csv", "--trials", "4")
    assert d4["deflated_sharpe"]["sharpe_hurdle"] < d16["deflated_sharpe"]["sharpe_hurdle"]
    assert d4["deflated_sharpe"]["trials_source"] == "--trials"


def test_dsr_skipped_without_a_return_count(root, capsys):
    _, d = run(capsys, root / "examples/results_converged.csv")
    assert "--n-obs" in d["deflated_sharpe"]["skipped"] and d["verdict"] == "CONVERGED"
    _, d = run(capsys, root / "examples/results_converged.csv", "--n-obs", "180")
    assert d["verdict"] == "ITERATE"


def test_report_shows_the_selection_bias_section_only_when_computed(root, capsys):
    cli.main(["convergence", str(root / "tests/fixtures/results_converged_180d.csv"), "--metric", "sharpe_oos"])
    out = capsys.readouterr().out
    assert "## Selection Bias" in out and "**Deflated Sharpe:** 0.81" in out
    assert "Converged on dispersion, stability and plateau, but deflated Sharpe" in out
    cli.main(["convergence", str(root / "examples/results_converged.csv"), "--metric", "sharpe_oos"])
    assert "Selection Bias" not in capsys.readouterr().out


def _folds_csv(root, overfit):
    """The converged example plus two IS/OOS folds. overfit: each fold's in-sample winner is worst out of sample."""
    lines = (root / "tests/fixtures/results_converged_2000d.csv").read_text().strip().splitlines()
    out = [lines[0] + ",sharpe_is_1,sharpe_oos_1,sharpe_is_2,sharpe_oos_2"]
    for i, line in enumerate(lines[1:]):
        is1, is2 = 1.0 + i * 0.01, 2.0 - i * 0.01  # fold 1 winner: last row; fold 2 winner: first row
        if overfit:
            oos1, oos2 = 1.0 - i * 0.01, 1.0 + i * 0.01  # winners land at the bottom
        else:
            oos1, oos2 = 1.0 + i * 0.01, 1.0 - i * 0.01  # winners stay on top
        out.append(f"{line},{is1:.2f},{oos1:.2f},{is2:.2f},{oos2:.2f}")
    return "\n".join(out) + "\n"


@pytest.mark.parametrize("overfit,pbo,verdict", [(True, 1.0, "ITERATE"), (False, 0.0, "CONVERGED")])
def test_pbo_from_fold_columns(root, tmp_path, capsys, overfit, pbo, verdict):
    p = tmp_path / "folds.csv"
    p.write_text(_folds_csv(root, overfit))
    rc, d = run(capsys, p)
    assert d["pbo"]["folds"] == 2 and d["pbo"]["pbo"] == pytest.approx(pbo)
    assert d["verdict"] == verdict
    assert set(d["parameters"]) == {"lookback", "threshold"}  # fold columns are not parameters
    assert d["metric"] == "sharpe_oos"


def test_pbo_skipped_without_folds(root, capsys):
    _, d = run(capsys, root / "examples/results_converged.csv")
    assert "fold columns" in d["pbo"]["skipped"]
