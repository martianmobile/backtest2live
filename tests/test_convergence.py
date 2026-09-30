import argparse

import pytest

from backtest2live import convergence as cv


def run(path, **kw):
    p = argparse.ArgumentParser()
    cv.add_arguments(p)
    args = p.parse_args([path])
    for k, v in kw.items():
        setattr(args, k, v)
    return cv.analyze(path, args)


def grid(rows):
    return "variant_id,lookback,threshold,sharpe_is,sharpe_oos,n_trades\n" + "\n".join(rows)


# --- metric resolution ------------------------------------------------------

def test_autodetect_prefers_oos(write_csv):
    ctx = run(write_csv(grid(["a,10,0.5,1.0,0.9,300", "b,20,0.5,1.2,1.1,300", "c,30,0.5,0.8,0.7,300"])))
    assert ctx["rank_col"] == "sharpe_oos"
    assert (ctx["is_col"], ctx["oos_col"]) == ("sharpe_is", "sharpe_oos")


def test_metric_prefix_resolves_to_oos(write_csv):
    ctx = run(write_csv(grid(["a,10,0.5,1.0,0.9,300", "b,20,0.5,1.2,1.1,300"])), metric="sharpe")
    assert ctx["rank_col"] == "sharpe_oos"


def test_unknown_metric_errors(write_csv):
    with pytest.raises(SystemExit, match="not found"):
        run(write_csv(grid(["a,10,0.5,1.0,0.9,300", "b,20,0.5,1.2,1.1,300"])), metric="sortino")


def test_lower_is_better_by_name(write_csv):
    ctx = run(write_csv("id,lookback,max_drawdown\na,1,0.2\nb,2,0.1\nc,3,0.3"), metric="max_drawdown")
    assert ctx["lower_better"] is True
    assert ctx["top"][0]["id"] == "b"


def test_higher_is_better_override(write_csv):
    ctx = run(write_csv("id,lookback,max_drawdown\na,1,0.2\nb,2,0.1\nc,3,0.3"),
              metric="max_drawdown", higher_is_better=True)
    assert ctx["top"][0]["id"] == "c"


@pytest.mark.xfail(strict=True, reason="keyword inference: 'window' matches 'win' — fixed by cardinality inference (#1)")
def test_window_param_is_not_a_metric(write_csv):
    ctx = run(write_csv("run_id,window,max_drawdown,n_trades\nr1,10,0.12,400\nr2,20,0.08,410\nr3,30,0.07,420"))
    assert ctx["rank_col"] == "max_drawdown"


# --- IS/OOS rank stability ---------------------------------------------------

def test_spearman_perfect_and_inverse():
    assert cv.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert cv.spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)


def test_spearman_ties_use_average_ranks():
    assert cv._avg_ranks([5, 1, 5, 3]) == [3.5, 1.0, 3.5, 2.0]
    assert cv.spearman([1, 1, 2], [1, 2, 3]) == pytest.approx(0.8660254, rel=1e-6)


def test_spearman_undefined_on_constant():
    assert cv.spearman([1, 1, 1], [1, 2, 3]) is None


def test_inverted_is_oos_kills(write_csv):
    rows = [f"v{i},{10 + 5 * i},0.5,{2.0 - 0.1 * i:.2f},{0.5 + 0.1 * i:.2f},300" for i in range(8)]
    ctx = run(write_csv(grid(rows)))
    assert ctx["verdict"] == "KILL"
    assert ctx["rho"] < 0


# --- parameter-space structure -------------------------------------------------

def test_plateau_is_contiguous(write_csv):
    rows = [f"v{i},{l},0.5,{s + 0.2:.2f},{s:.2f},300"
            for i, (l, s) in enumerate([(10, 1.00), (20, 1.50), (30, 1.52), (40, 1.51), (50, 1.49), (60, 1.48), (70, 1.10)])]
    ctx = run(write_csv(grid(rows)), top_k=3)
    assert ctx["params"]["lookback"]["contiguous"] is True
    assert ctx["params"]["lookback"]["edge"] is False


def test_isolated_winners_are_scattered(write_csv):
    vals = [1.50, 0.40, 0.45, 0.50, 1.49, 0.42, 0.44, 1.48]
    rows = [f"v{i},{10 * (i + 1)},0.5,{s + 0.1:.2f},{s:.2f},300" for i, s in enumerate(vals)]
    ctx = run(write_csv(grid(rows)), top_k=3)
    assert ctx["params"]["lookback"]["contiguous"] is False
    assert ctx["plateau"] is False
    assert ctx["verdict"] != "CONVERGED"


def test_edge_peak_iterates(write_csv):
    vals = [1.00, 1.10, 1.20, 1.30, 1.40, 1.41, 1.42]
    rows = [f"v{i},{10 * (i + 1)},0.5,{s + 0.1:.2f},{s:.2f},300" for i, s in enumerate(vals)]
    ctx = run(write_csv(grid(rows)), top_k=3)
    assert ctx["params"]["lookback"]["edge"] is True
    assert ctx["verdict"] == "ITERATE"


def test_single_value_grid_is_trivially_contiguous(write_csv):
    ctx = run(write_csv(grid(["a,10,0.5,1.0,0.9,300", "b,20,0.5,1.2,1.1,300", "c,30,0.5,0.8,0.7,300"])))
    assert ctx["params"]["threshold"] == {"grid": [0.5], "contiguous": True, "edge": False, "occupied": [0.5]}


# --- samples, dispersion, separation ----------------------------------------

def test_low_samples_block_convergence(root):
    ctx = run(str(root / "examples/results_converged.csv"), metric="sharpe_oos", min_samples=10_000)
    assert ctx["samples_ok"] is False
    assert ctx["verdict"] == "ITERATE"


def test_relative_dispersion_edge_cases():
    assert cv.relative_dispersion([1.0]) is None
    assert cv.relative_dispersion([1.0, -1.0]) is None  # mean ~ 0
    assert cv.relative_dispersion([1.0, 1.0, None]) == 0.0


def test_best_equal_to_median_kills(write_csv):
    ctx = run(write_csv("id,lookback,score\na,1,1.0\nb,2,1.0\nc,3,1.0"), metric="score")
    assert ctx["separation"] == 0.0
    assert ctx["verdict"] == "KILL"


# --- bad input ---------------------------------------------------------------

def test_header_only_file_errors(write_csv):
    with pytest.raises(SystemExit, match="no data rows"):
        run(write_csv("variant_id,sharpe"))


def test_no_numeric_columns_errors(write_csv):
    with pytest.raises(SystemExit, match="no numeric"):
        run(write_csv("variant_id,label\na,x\nb,y"))


def test_single_variant_errors(write_csv):
    with pytest.raises(SystemExit, match="need >=2"):
        run(write_csv("variant_id,sharpe\na,1.0"))


def test_garbage_cells_drop_the_column(write_csv):
    ctx = run(write_csv("id,lookback,sharpe,notes\na,1,1.0,x\nb,2,2.0,1\nc,3,0.5,y"))
    assert ctx["rank_col"] == "sharpe"
    assert "notes" not in ctx["params"]
