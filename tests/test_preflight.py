import json

import pytest

from backtest2live import cli

T = 1_711_756_800_000  # 2024-03-30T00:00:00Z, ms


def run(capsys, *argv):
    rc = cli.main(["preflight", *argv, "--json"])
    return rc, json.loads(capsys.readouterr().out)


def section(doc, name):
    return next(s for s in doc["sections"] if s["section"] == name)


def test_converged_sweep_passes_and_the_rest_is_not_run(root, capsys):
    rc, d = run(capsys, "--results", str(root / "examples/results_converged.csv"), "--metric", "sharpe_oos")
    assert rc == 0 and d["status"] == "pass"
    oos = section(d, "out_of_sample")
    assert oos["status"] == "pass" and oos["checks"][0]["verdict"] == "CONVERGED"
    assert section(d, "execution")["status"] == "not run"
    assert [section(d, s)["status"] for s in ("capacity", "risk")] == ["not run", "not run"]
    assert all(s["limits"] for s in d["sections"])


def test_iterate_warns_kill_fails(root, capsys):
    assert run(capsys, "--results", str(root / "examples/results_iterate.csv"), "--metric", "sharpe_oos")[0] == 1
    rc, d = run(capsys, "--results", str(root / "tests/fixtures/results_kill.csv"), "--metric", "sharpe_oos")
    assert rc == 2 and d["status"] == "fail"


def test_nothing_to_check_is_an_error(capsys):
    assert cli.main(["preflight"]) == cli.EXIT_ERROR


def test_markdown_lists_every_section(root, capsys):
    cli.main(["preflight", "--results", str(root / "examples/results_converged.csv"), "--metric", "sharpe_oos"])
    out = capsys.readouterr().out
    for title in ("Out-of-sample: PASS", "Execution realism: NOT RUN", "Capacity: NOT RUN", "Risk: NOT RUN"):
        assert f"## {title}" in out
    assert "cannot see" in out


def test_out_writes_md_and_json(root, tmp_path, capsys):
    prefix = str(tmp_path / "pf")
    cli.main(["preflight", "--results", str(root / "examples/results_converged.csv"), "--out", prefix])
    capsys.readouterr()
    assert json.loads((tmp_path / "pf.json").read_text())["schema"] == "backtest2live.preflight/1"
    assert (tmp_path / "pf.md").read_text().startswith("# Preflight")


# --- execution realism, on the fill-gap test market ------------------------------------

BOOK = "ts,bid_px,bid_qty,ask_px,ask_qty\n1000,100.0,5,100.1,5\n5000,99.9,2,100.1,5\n8000,100.0,1,100.1,4\n20000,100.0,1,100.1,4"
TRADES = "ts,price,qty,is_buyer_maker\n2000,100.0,3,true\n3000,100.0,2.5,true\n4000,100.1,1,false\n12000,100.1,4.5,false"


def _abs(text, cols=(0,)):
    out = []
    for line in text.strip().splitlines():
        c = line.split(",")
        for i in cols:
            if i < len(c) and c[i].isdigit():
                c[i] = str(T + int(c[i]))
        out.append(",".join(c))
    return "\n".join(out)


@pytest.fixture
def market(write_csv):
    pytest.importorskip("numpy")
    pytest.importorskip("pandas")
    return ["--book", write_csv(_abs(BOOK), "book.csv"), "--trades", write_csv(_abs(TRADES), "trades.csv")]


def test_untyped_resting_log_runs_fill_gap_only(write_csv, market, capsys):
    log = _abs("order_id,ts,side,price,size,end_ts,filled\na,1500,buy,100.0,0.1,4500,1\nb,1500,buy,100.0,1,4500,1",
               cols=(1, 5))
    rc, d = run(capsys, "--orders", write_csv(log, "o.csv"), *market)
    fg, cg = section(d, "execution")["checks"]
    assert fg["verdict"] == "OVERSTATED" and fg["status"] == "fail" and rc == 2
    assert cg["status"] == "not run" and "no `type` column" in cg["reason"]


def test_typed_log_runs_both(write_csv, market, capsys):
    log = _abs("order_id,ts,side,type,price,size,end_ts,filled,fill_price,pnl\n"
               "a,1500,buy,limit,100.0,0.1,4500,1,,1\n"
               "m,2500,buy,market,,1,2500,1,100.05,1", cols=(1, 6))
    rc, d = run(capsys, "--orders", write_csv(log, "o.csv"), *market)
    fg, cg = section(d, "execution")["checks"]
    assert cg["verdict"] in ("CONSISTENT", "OVERSTATED") and cg["result"]["counts"]["taker"] == 1
    # fill-gap reads a mixed log once it skips market rows (PR #34); until then it says why it did not run
    assert fg["status"] == "pass" or (fg["status"] == "not run" and "price" in fg["reason"]), fg


def test_a_check_that_cannot_run_says_why(write_csv, capsys):
    pytest.importorskip("pandas")
    log = _abs("ts,side,size\n1500,buy,1")
    rc, d = run(capsys, "--orders", write_csv(log, "o.csv"))  # no market data
    fg, cg = section(d, "execution")["checks"]
    assert cg["status"] == "not run" and "--venue" in cg["reason"]
