import json

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("pandas")

from backtest2live import cli  # noqa: E402

T = 1_711_756_800_000  # 2024-03-30T00:00:00Z in ms; times below are offsets from it


def _abs(text):
    """Turn leading 'ts' offsets (and end_ts offsets) into epoch ms."""
    out = []
    for line in text.strip().splitlines():
        cells = line.split(",")
        if cells[0].isdigit():
            cells[0] = str(T + int(cells[0]))
        out.append(",".join(cells))
    return "\n".join(out)


# One synthetic market, ms timestamps (offsets from T). Bid 100.0 x 5 / ask 100.1 x 5 until t=5000,
# then the bid drops to 99.9 x 2 at t=5000, and back to 100.0 x 1 at t=8000.
BOOK = """ts,bid_px,bid_qty,ask_px,ask_qty
1000,100.0,5,100.1,5
5000,99.9,2,100.1,5
8000,100.0,1,100.1,4
20000,100.0,1,100.1,4
"""
# is_buyer_maker=true: a seller hit the bid.
TRADES = """ts,price,qty,is_buyer_maker
2000,100.0,3,true
3000,100.0,2.5,true
4000,100.1,1,false
9000,99.9,1,true
12000,100.1,4.5,false
"""


@pytest.fixture
def market(write_csv):
    return ["--book", write_csv(_abs(BOOK), "book.csv"), "--trades", write_csv(_abs(TRADES), "trades.csv")]


def run(capsys, orders_path, market, *extra):
    rc = cli.main(["fill-gap", orders_path, *market, "--json", *extra])
    return rc, json.loads(capsys.readouterr().out)


def orders(rows, header="order_id,ts,side,price,size,end_ts,filled"):
    """Order rows with ts/end_ts as offsets from T (columns 2 and 6)."""
    out = [header]
    for r in rows:
        c = r.split(",")
        for i in (1, 5):
            if i < len(c) and c[i].isdigit():
                c[i] = str(T + int(c[i]))
        out.append(",".join(c))
    return "\n".join(out)


def test_queue_proxy_vs_touch(write_csv, market, capsys):
    p = write_csv(orders([
        # joins the bid at 100.0 behind 5: 5.5 prints at 100.0 cover 5 + 0.1 -> proxy fill
        "a,1500,buy,100.0,0.1,4500,1",
        # same but size 1: 5.5 < 5 + 1 -> touch fill only
        "b,1500,buy,100.0,1,4500,1",
        # joins the ask at 100.1 behind 5; only 1 lifted before the end -> touch fill only
        "c,1500,sell,100.1,0.5,4500,1",
    ]), "orders.csv")
    rc, d = run(capsys, p, market)
    assert d["counts"]["passive"] == 3
    assert d["counts"]["touch_fills"] == 3
    assert d["counts"]["proxy_fills"] == 1
    assert d["counts"]["missed"] == 2
    assert d["verdict"] == "OVERSTATED" and rc == 1
    assert d["missed_ids"] == ["b", "c"]


def test_improving_order_has_no_queue(write_csv, market, capsys):
    # buy at 100.05 inside the spread: first print at or through it fills. Tick must allow it.
    p = write_csv(orders(["a,1500,buy,100.05,1,4500,1"]), "orders.csv")
    rc, d = run(capsys, p, market, "--tick", "0.05")
    assert d["counts"]["proxy_fills"] == 1  # 3 printed at 100.0, through 100.05


def test_marketable_is_skipped(write_csv, market, capsys):
    p = write_csv(orders(["a,1500,buy,100.1,1,4500,1", "b,1500,buy,100.0,0.1,4500,1"]), "orders.csv")
    rc, d = run(capsys, p, market)
    assert d["counts"]["marketable"] == 1 and d["counts"]["passive"] == 1


def test_behind_the_touch_waits_for_the_price(write_csv, market, capsys):
    # buy 99.9 at t=1500 rests behind the 100.0 bid; the bid reaches 99.9 x 2 at 5000,
    # then 1 prints at 99.9 at 9000: queue 2 + 0.5 not covered -> no proxy fill.
    # The bid moves back up at 8000, so 99.9 prints only once.
    p = write_csv(orders(["a,1500,buy,99.9,0.5,15000,1", "b,1500,buy,99.8,0.5,4000,0"]), "orders.csv")
    rc, d = run(capsys, p, market)
    assert d["counts"]["never_reached"] == 1  # b: the bid never came down to 99.8 before 4000
    assert d["counts"]["touch_fills"] == 1 and d["counts"]["proxy_fills"] == 0


def test_print_through_the_price_fills(write_csv, market, capsys):
    # sell at 100.1 behind 5 at t=1500, resting until 15000: at 12000, 4.5 prints at 100.1,
    # 1 already printed at 4000 -> 5.5 >= 5 + 0.5 -> fill. Mirror of the buy logic.
    p = write_csv(orders(["a,1500,sell,100.1,0.5,15000,1"]), "orders.csv")
    rc, d = run(capsys, p, market)
    assert d["counts"]["proxy_fills"] == 1 and rc == 0 and d["verdict"] == "CONSISTENT"


def test_through_trade_counts(write_csv, capsys):
    book = write_csv(_abs("ts,bid_px,bid_qty,ask_px,ask_qty\n1000,100.0,50,100.1,5\n3000,99.8,1,100.1,5"), "book.csv")
    trades = write_csv(_abs("ts,price,qty,is_buyer_maker\n3000,100.0,1,true\n3000,99.9,2,true"), "trades.csv")
    p = write_csv(orders(["a,1500,buy,100.0,1,5000,1"]), "orders.csv")
    rc, d = run(capsys, p, ["--book", book, "--trades", trades])
    # only 1 printed at 100.0 against 50 ahead, but a sell printed at 99.9: the level was cleared
    assert d["counts"]["proxy_fills"] == 1


def test_latency_delays_arrival(write_csv, market, capsys):
    p = write_csv(orders(["a,1500,buy,100.0,0.1,2500,1"]), "orders.csv")
    _, d0 = run(capsys, p, market)
    _, d1 = run(capsys, p, market, "--latency-ms", "600")  # arrives at 2100, after the 2000 print
    assert d0["counts"]["touch_fills"] == 1 and d1["counts"]["touch_fills"] == 0


def test_no_fill_flag_uses_touch_rule_and_max_rest(write_csv, market, capsys):
    p = write_csv(f"ts,side,price,size\n{T + 1500},buy,100.0,1\n", "orders.csv")
    rc, d = run(capsys, p, market, "--max-rest", "3")
    assert d["has_claim"] is False and d["order_end"] == "--max-rest 3s"
    assert d["counts"]["baseline_fills"] == 1 and d["counts"]["proxy_fills"] == 0


def test_pnl_on_missed_fills(write_csv, market, capsys):
    p = write_csv(orders(["a,1500,buy,100.0,0.1,4500,1,2.0", "b,1500,buy,100.0,1,4500,1,3.0"],
                         header="order_id,ts,side,price,size,end_ts,filled,pnl"), "orders.csv")
    rc, d = run(capsys, p, market)
    assert d["pnl"] == {"baseline_fills": 5.0, "missed_fills": 3.0}


def test_iso_timestamps(write_csv, market, capsys):
    p = write_csv(orders(["a,2024-03-30T00:00:01.500Z,buy,100.0,0.1,2024-03-30T00:00:04.500Z,1"]), "orders.csv")
    rc, d = run(capsys, p, market)
    assert d["counts"]["proxy_fills"] == 1


def test_markdown_report(write_csv, market, capsys):
    p = write_csv(orders(["a,1500,buy,100.0,0.1,4500,1", "b,1500,buy,100.0,1,4500,1"]), "orders.csv")
    rc = cli.main(["fill-gap", p, *market])
    out = capsys.readouterr().out
    assert "## Verdict: OVERSTATED" in out
    assert "martianmobile.com/fill-autopsy?utm_source=oss" in out


@pytest.mark.parametrize("csv,msg", [
    ("ts,side,size\n1,buy,1\n", "missing columns"),
    ("ts,side,price,size\n1,up,1,1\n", "side must be buy/sell"),
    ("ts,side,price,size,filled\n1,buy,1,1,maybe\n", "true/false"),
])
def test_bad_order_log(write_csv, market, capsys, csv, msg):
    p = write_csv(csv, "orders.csv")
    assert cli.main(["fill-gap", p, *market]) == cli.EXIT_ERROR
    assert msg in capsys.readouterr().err


def test_source_is_required(write_csv, capsys):
    p = write_csv("ts,side,price,size\n1,buy,1,1\n", "orders.csv")
    assert cli.main(["fill-gap", p]) == cli.EXIT_ERROR
    assert "--venue" in capsys.readouterr().err


def test_binance_archive_ends_2024_03_30(write_csv, capsys):
    p = write_csv("ts,side,price,size\n2025-01-01T00:00:00Z,buy,1,1\n", "orders.csv")
    assert cli.main(["fill-gap", p, "--venue", "binance-um", "--pair", "BTCUSDT", "--offline"]) == cli.EXIT_ERROR
    assert "--book and --trades" in capsys.readouterr().err
