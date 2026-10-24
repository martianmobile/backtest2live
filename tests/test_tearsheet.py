import json

import pytest

from backtest2live import cli, tearsheet

DAY = 86_400_000
T0 = 1_704_067_200_000  # 2024-01-01T00:00:00Z


def equity_csv(values):
    return "ts,equity\n" + "\n".join(f"{T0 + i * DAY},{v}" for i, v in enumerate(values))


def sheet(capsys, path, *extra):
    rc = cli.main(["tearsheet", path, "--json", *extra])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def test_equity_curve_metrics(write_csv, capsys):
    # 100 → 110 → 99 → 121: +10%, -10%, +22.2%; drawdown 10% from the 110 peak, one day under water
    d = sheet(capsys, write_csv(equity_csv([100, 110, 99, 121])), "--provenance", "backtest")
    m = d["metrics"]
    assert m["start_equity"] == 100 and m["end_equity"] == 121
    assert m["total_return"] == pytest.approx(0.21)
    assert m["max_drawdown"] == pytest.approx(-0.10) and m["max_drawdown_days"] == 1
    assert m["worst"]["day"]["return"] == pytest.approx(-0.10) and m["worst"]["day"]["ending"] == "2024-01-03"
    assert m["monthly"] == {"2024-01": pytest.approx(0.21)}
    assert m["days"] == 3  # the first row is the starting point
    assert m["sharpe"] is None and m["cagr"] is None  # too short to annualize


def test_sharpe_and_cagr_on_a_long_enough_curve(write_csv, capsys):
    import statistics
    vals, e = [100.0], 100.0
    for i in range(40):
        e *= 1.01 if i % 2 else 0.995
        vals.append(e)
    m = sheet(capsys, write_csv(equity_csv(vals)), "--provenance", "backtest")["metrics"]
    rets = [vals[i] / vals[i - 1] - 1 for i in range(1, len(vals))]
    assert m["sharpe"] == pytest.approx(statistics.mean(rets) / statistics.stdev(rets) * 365 ** 0.5)
    assert m["cagr"] == pytest.approx((vals[-1] / 100) ** (365 / 40) - 1)


def test_provenance_label_is_in_the_header(write_csv, capsys):
    p = write_csv(equity_csv([100, 101, 102]))
    cli.main(["tearsheet", p, "--provenance", "backtest", "--assumption", "fills at touch"])
    out = capsys.readouterr().out
    assert out.splitlines()[2] == "**Backtest — simulated fills, assumptions listed below**"
    assert "- fills at touch" in out and "Generated with Backtest2Live" in out


def test_provenance_is_required(write_csv):
    with pytest.raises(SystemExit) as e:
        cli.main(["tearsheet", write_csv(equity_csv([100, 101]))])
    assert e.value.code == cli.EXIT_ERROR


def test_self_reported_names_the_venue(write_csv, capsys):
    p = write_csv(equity_csv([100, 101, 102]))
    assert cli.main(["tearsheet", p, "--provenance", "self-reported"]) == cli.EXIT_ERROR
    assert "--venue" in capsys.readouterr().err
    d = sheet(capsys, p, "--provenance", "self-reported", "--venue", "Bybit")
    assert d["provenance"]["label"].startswith("Live, self-reported — read from Bybit trade history")


def test_backtest_without_assumptions_says_so(write_csv, capsys):
    cli.main(["tearsheet", write_csv(equity_csv([100, 101])), "--provenance", "backtest"])
    assert "fill model behind these numbers is not stated" in capsys.readouterr().out


def test_order_log_needs_capital_and_gives_trade_metrics(write_csv, capsys):
    log = ("ts,side,fill_qty,fill_price,fee,pnl\n"
           f"{T0},buy,1,100,0.1,0\n"
           f"{T0 + DAY // 2},sell,1,110,0.1,10\n"
           f"{T0 + DAY},buy,1,105,0.1,0\n"
           f"{T0 + 2 * DAY},sell,1,100,0.1,-5\n")
    p = write_csv(log)
    assert cli.main(["tearsheet", p, "--provenance", "backtest"]) == cli.EXIT_ERROR
    capsys.readouterr()
    m = sheet(capsys, p, "--provenance", "backtest", "--capital", "1000")["metrics"]
    assert m["end_equity"] == pytest.approx(1005)
    assert m["profit_factor"] == pytest.approx(2.0)
    assert m["fees"] == pytest.approx(0.4)
    assert m["fee_rate_bps"] == pytest.approx(0.4 / 415 * 1e4)
    assert m["exposure"] == pytest.approx(0.75)  # long day 0-0.5 and 1-2 of a 2-day span


def test_out_writes_html_and_json_with_a_hash(write_csv, tmp_path, capsys):
    prefix = str(tmp_path / "sheet")
    cli.main(["tearsheet", write_csv(equity_csv([100, 105, 103])), "--provenance", "backtest", "--out", prefix])
    capsys.readouterr()
    doc = json.loads((tmp_path / "sheet.json").read_text())
    page = (tmp_path / "sheet.html").read_text()
    assert "Backtest — simulated fills" in page and doc["sha256"] in page and "<svg" in page
    # the hash is over the sheet without the hash field, so anyone can recompute it
    import hashlib
    body = {k: v for k, v in doc.items() if k != "sha256"}
    raw = json.dumps(body, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    assert hashlib.sha256(raw).hexdigest() == doc["sha256"]


def test_cites_a_check_result(write_csv, tmp_path, capsys):
    chk = tmp_path / "fg.json"
    chk.write_text(json.dumps({"schema": "backtest2live.fill-gap/1", "verdict": "OVERSTATED"}))
    d = sheet(capsys, write_csv(equity_csv([100, 101])), "--provenance", "backtest", "--check", str(chk))
    assert d["checks"] == [{"schema": "backtest2live.fill-gap/1", "verdict": "OVERSTATED", "file": str(chk)}]


def test_iso_dates_and_gaps_fill_forward(write_csv, capsys):
    p = write_csv("date,value\n2024-01-01,100\n2024-01-04,103\n")
    m = sheet(capsys, p, "--provenance", "backtest")["metrics"]
    assert m["days"] == 3 and m["total_return"] == pytest.approx(0.03)


def test_bad_input_is_an_error(write_csv, capsys):
    assert cli.main(["tearsheet", write_csv("ts,foo\n1,2\n"), "--provenance", "backtest"]) == cli.EXIT_ERROR
    assert "neither an equity column" in capsys.readouterr().err


def test_schema_constant():
    assert tearsheet.SCHEMA == "backtest2live.tearsheet/1"
