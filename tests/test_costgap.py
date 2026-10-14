import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pandas")

from backtest2live import cli  # noqa: E402
from conftest import T, abs_ms  # noqa: E402

# bid 100.0 x 5 / ask 100.1 x 2 from t=1000; at t=5000 the ask thins to 100.1 x 0.5; at 8000 ask 100.3 x 4
BOOK = """ts,bid_px,bid_qty,ask_px,ask_qty
1000,100.0,5,100.1,2
5000,100.0,5,100.1,0.5
8000,100.0,5,100.3,4
"""


@pytest.fixture
def book(write_csv):
    return ["--book", write_csv(abs_ms(BOOK), "book.csv")]


def run(capsys, orders_path, book, *extra):
    rc = cli.main(["cost-gap", orders_path, *book, "--json", *extra])
    return rc, json.loads(capsys.readouterr().out)


def orders(rows, header="order_id,ts,side,size,type,price,fill_price"):
    out = [header]
    for r in rows:
        c = r.split(",")
        if c[1].isdigit():
            c[1] = str(T + int(c[1]))
        out.append(",".join(c))
    return "\n".join(out)


def test_market_buy_within_touch_pays_the_spread(write_csv, book, capsys):
    # backtest booked the mid (100.05); the real ask is 100.1 -> +5 bps on 100.05
    p = write_csv(orders(["a,2000,buy,1,market,,100.05"]), "o.csv")
    rc, d = run(capsys, p, book)
    assert d["counts"]["taker"] == 1 and d["counts"]["beyond_touch"] == 0
    assert d["cost_bps"]["mean"] == pytest.approx((100.1 - 100.05) / 100.05 * 1e4, rel=1e-6)
    assert d["verdict"] == "REPORTED" and rc == 0


def test_baseline_defaults_to_mid_at_arrival(write_csv, book, capsys):
    p = write_csv(orders(["a,2000,sell,1,market,"], header="order_id,ts,side,size,type,price"), "o.csv")
    rc, d = run(capsys, p, book)
    # sell hits the bid 100.0; mid is 100.05 -> +5 bps cost
    assert d["baseline"] == "mid at arrival"
    assert d["cost_bps"]["mean"] == pytest.approx((100.05 - 100.0) / 100.05 * 1e4, rel=1e-6)


def test_size_beyond_touch_walks_one_tick_per_slice(write_csv, book, capsys):
    # ask 100.1 x 2; buy 5: 2 @100.1, 2 @100.2, 1 @100.3 -> vwap 100.18
    p = write_csv(orders(["a,2000,buy,5,market,,100.1"]), "o.csv")
    rc, d = run(capsys, p, book, "--tick", "0.1")
    assert d["counts"]["beyond_touch"] == 1
    vwap = (2 * 100.1 + 2 * 100.2 + 1 * 100.3) / 5
    assert d["cost_bps"]["mean"] == pytest.approx((vwap - 100.1) / 100.1 * 1e4, rel=1e-6)


def test_marketable_limit_is_capped_and_resting_limit_is_skipped(write_csv, book, capsys):
    p = write_csv(orders([
        "cap,2000,buy,5,limit,100.2,100.1",   # crosses; walks to 100.2 at most: 2 @100.1 + 2 @100.2, 1 unfilled
        "rest,2000,buy,1,limit,100.0,100.0",  # at the bid: rests, fill-gap's job
    ]), "o.csv")
    rc, d = run(capsys, p, book, "--tick", "0.1")
    assert d["counts"] ["taker"] == 1 and d["counts"]["resting"] == 1
    assert d["counts"]["partially_unfilled"] == 1
    vwap = (2 * 100.1 + 2 * 100.2) / 4
    assert d["cost_bps"]["mean"] == pytest.approx((vwap - 100.1) / 100.1 * 1e4, rel=1e-6)


def test_market_order_and_equivalent_limit_price_identically(write_csv, book, capsys):
    p = write_csv(orders(["m,2000,buy,1,market,,100.05", "l,2000,buy,1,limit,100.1,100.05"]), "o.csv")
    rc, d = run(capsys, p, book)
    assert d["cost_bps"]["median"] == pytest.approx(d["cost_bps"]["mean"])
    assert d["counts"]["taker"] == 2


def test_latency_reads_a_later_book(write_csv, book, capsys):
    # at 2000 the ask shows 2; arriving 3.5 s later it shows 0.5, so a 1-lot walks a tick
    p = write_csv(orders(["a,2000,buy,1,market,,100.1"]), "o.csv")
    _, d0 = run(capsys, p, book, "--tick", "0.1")
    _, d1 = run(capsys, p, book, "--tick", "0.1", "--latency-ms", "3500")
    assert d0["cost_bps"]["mean"] == pytest.approx(0.0, abs=1e-9)
    assert d1["cost_bps"]["mean"] > 0
    assert [row["latency_ms"] for row in d1["latency_sweep"]] == [0, 5, 20, 100]


def test_pnl_verdict(write_csv, book, capsys):
    # cost 0.05 on 100-notional orders; pnl 0.10 each -> 50% of pnl removed -> OVERSTATED at 25%
    rows = [f"o{i},2000,buy,1,market,,100.05,0.10" for i in range(4)]
    p = write_csv(orders(rows, header="order_id,ts,side,size,type,price,fill_price,pnl"), "o.csv")
    rc, d = run(capsys, p, book)
    assert d["drag"]["share_of_pnl"] == pytest.approx(0.5, rel=1e-6)
    assert d["verdict"] == "OVERSTATED" and rc == 1
    rc, d = run(capsys, p, book, "--tolerance", "0.6")
    assert d["verdict"] == "CONSISTENT" and rc == 0


def test_no_price_column_means_all_market(write_csv, book, capsys):
    p = write_csv(f"ts,side,size\n{T + 2000},buy,1\n{T + 2000},sell,1\n", "o.csv")
    rc, d = run(capsys, p, book)
    assert d["counts"]["taker"] == 2


def test_markdown_report(write_csv, book, capsys):
    p = write_csv(orders(["a,2000,buy,1,market,,100.05"]), "o.csv")
    assert cli.main(["cost-gap", p, *book]) == 0
    out = capsys.readouterr().out
    assert "## Verdict: REPORTED" in out and "Cost by arrival latency" in out
    assert "utm_campaign=cost-gap" in out


@pytest.mark.parametrize("csv,msg", [
    ("ts,side,size,type,price\n1,buy,1,limit,\n", "limit order needs a price"),
    ("ts,side,size,type\n1,buy,1,stop\n", "type must be market/limit"),
    ("ts,size\n1,1\n", "missing columns"),
])
def test_bad_order_log(write_csv, book, capsys, csv, msg):
    assert cli.main(["cost-gap", write_csv(csv, "o.csv"), *book]) == cli.EXIT_ERROR
    assert msg in capsys.readouterr().err


def test_source_is_required(write_csv, capsys):
    p = write_csv(f"ts,side,size\n{T + 2000},buy,1\n", "o.csv")
    assert cli.main(["cost-gap", p]) == cli.EXIT_ERROR
