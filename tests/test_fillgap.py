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


def test_sell_volume_at_price_fills(write_csv, market, capsys):
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


def test_sell_through_trade_counts(write_csv, capsys):
    # mirror: a resting sell at 100.1 behind 50; buys print at 100.1 x1 and 100.2 x2 -> level cleared
    book = write_csv(_abs("ts,bid_px,bid_qty,ask_px,ask_qty\n1000,100.0,5,100.1,50\n3000,100.0,5,100.3,1"), "book.csv")
    trades = write_csv(_abs("ts,price,qty,is_buyer_maker\n3000,100.1,1,false\n3000,100.2,2,false"), "trades.csv")
    p = write_csv(orders(["a,1500,sell,100.1,1,5000,1"]), "orders.csv")
    rc, d = run(capsys, p, ["--book", book, "--trades", trades])
    assert d["counts"]["proxy_fills"] == 1


@pytest.mark.parametrize("trade_ts", [3000, 2999])
def test_behind_order_swept_through_fills(write_csv, capsys, trade_ts):
    # buy 99.9 rests behind the 100.0 bid. A sweep prints 100.0, 99.9, 99.8 in the same ms as
    # the book row that shows the move (or 1 ms before it): every bid at 99.9 filled, ours included.
    book = write_csv(_abs("ts,bid_px,bid_qty,ask_px,ask_qty\n1000,100.0,5,100.1,5\n3000,99.7,1,99.8,2"), "book.csv")
    trades = write_csv(_abs(f"ts,price,qty,is_buyer_maker\n{trade_ts},100.0,5,true\n{trade_ts},99.9,3,true\n"
                            f"{trade_ts},99.8,4,true"), "trades.csv")
    p = write_csv(orders(["behind,1500,buy,99.9,0.5,8000,1"]), "orders.csv")
    rc, d = run(capsys, p, ["--book", book, "--trades", trades])
    assert d["counts"]["proxy_fills"] == 1 and d["counts"]["never_reached"] == 0


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


def test_mostly_off_grid_prices_mean_a_wrong_tick(write_csv, market, capsys):
    p = write_csv(orders(["a,1500,buy,100.05,1,4500,1", "b,1500,buy,100.15,1,4500,1"]), "orders.csv")
    assert cli.main(["fill-gap", p, *market]) == cli.EXIT_ERROR
    assert "100.0% of order prices are not on the 0.1 tick grid" in capsys.readouterr().err


def test_one_malformed_price_in_many_is_snapped(write_csv, market, capsys):
    # the public archive carries the odd malformed row; one in 200 must not reject the log
    rows = [f"o{i},1500,buy,100.0,0.001,4500,1" for i in range(199)] + ["bad,1500,buy,100.04,0.001,4500,1"]
    cli.main(["fill-gap", write_csv(orders(rows), "orders.csv"), *market, "--json"])
    captured = capsys.readouterr()
    assert json.loads(captured.out)["counts"]["passive"] == 200  # 100.04 snapped to 100.0
    assert "1 of 200 order prices off the 0.1 tick grid; snapped" in captured.err


def test_missing_book_file_is_an_error(write_csv, capsys):
    p = write_csv(f"ts,side,price,size\n{T + 1500},buy,100.0,1\n", "orders.csv")
    t = write_csv(_abs(TRADES), "trades.csv")
    assert cli.main(["fill-gap", p, "--book", "/nonexistent/book.csv", "--trades", t]) == cli.EXIT_ERROR
    assert "book file not found" in capsys.readouterr().err


@pytest.mark.parametrize("csv,msg", [
    ("ts,side,size\n1,buy,1\n", "missing columns"),
    ("ts,side,price,size\n1,up,1,1\n", "side must be buy/sell"),
    ("ts,side,price,size,filled\n1,buy,1,1,maybe\n", "true/false"),
    ("ts,side,price,size\n1,buy,abc,1\n", "non-numeric price"),
    ("ts,side,price,size,end_ts\n1,buy,1,1,2\n3,buy,1,1,\n", "blank or non-numeric timestamp"),
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


# --- Binance archive path -------------------------------------------------------

import hashlib  # noqa: E402
import io  # noqa: E402
import zipfile  # noqa: E402

from backtest2live.fillgap import data  # noqa: E402

DAY = "2024-03-30"


def _zip_bytes(name, text):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
    return buf.getvalue()


def _archive(tmp_path, monkeypatch):
    """A tiny synthetic bookTicker + aggTrades day in a private cache. Returns the cache root."""
    monkeypatch.setenv("BT2LIVE_CACHE", str(tmp_path / "cache"))
    base = tmp_path / "cache" / "data.binance.vision" / "data" / "futures" / "um" / "daily"
    book = ("update_id,best_bid_price,best_bid_qty,best_ask_price,best_ask_qty,transaction_time,event_time\n"
            f"1,100.0,5,100.1,5,{T + 1000},{T + 1000}\n"
            f"2,100.0,1,100.1,5,{T + 86_399_900},{T + 86_399_900}\n")
    trades = ("agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
              f"1,100.0,3,1,1,{T + 2000},true\n"
              f"2,100.0,2.5,2,2,{T + 3000},true\n"
              f"3,100.1,1,3,3,{T + 4000},false\n")
    for kind, text in (("bookTicker", book), ("aggTrades", trades)):
        d = base / kind / "BTCUSDT"
        d.mkdir(parents=True)
        (d / f"BTCUSDT-{kind}-{DAY}.zip").write_bytes(_zip_bytes(f"BTCUSDT-{kind}-{DAY}.csv", text))
    return base


def test_load_binance_um_parses_archive(tmp_path, monkeypatch):
    _archive(tmp_path, monkeypatch)
    from datetime import date
    book, trades = data.load_binance_um("BTCUSDT", [date(2024, 3, 30)], offline=True)
    assert book["t"][0] == (T + 1000) * 1000           # ms -> us
    assert list(trades["sell_aggr"]) == [True, True, False]
    assert trades["qty"][1] == 2.5


def test_venue_run_and_midnight_tail(tmp_path, monkeypatch, write_csv, capsys):
    # an order at 23:59:30 with --max-rest 60 rests into 03-31, past the archive: evaluated on 03-30's data
    _archive(tmp_path, monkeypatch)
    p = write_csv(f"order_id,ts,side,price,size,filled\na,{T + 1500},buy,100.0,0.1,1\n"
                  f"late,{T + 86_370_000},buy,100.0,0.1,0\n", "orders.csv")
    rc, d = run(capsys, p, ["--venue", "binance-um", "--pair", "BTCUSDT", "--offline"])
    assert d["counts"]["passive"] == 2 and d["counts"]["proxy_fills"] == 1
    assert d["source"].startswith("Binance")


def test_fetch_verifies_checksum_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("BT2LIVE_CACHE", str(tmp_path / "cache"))
    payload = _zip_bytes("x.csv", "a,b\n1,2\n")
    good = hashlib.sha256(payload).hexdigest()
    calls = []

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(url, timeout=0):
        calls.append(url)
        if url.endswith(".CHECKSUM"):
            return Resp(f"{fake_urlopen.digest}  file.zip\n".encode())
        return Resp(payload)

    monkeypatch.setattr(data.urllib.request, "urlopen", fake_urlopen)
    from datetime import date
    day = date(2024, 3, 30)

    fake_urlopen.digest = "0" * 64
    with pytest.raises(SystemExit, match="checksum mismatch"):
        data._fetch("bookTicker", "BTCUSDT", day, False, lambda m: None)
    assert not list((tmp_path / "cache").rglob("*.part"))

    fake_urlopen.digest = good
    path = data._fetch("bookTicker", "BTCUSDT", day, False, lambda m: None)
    assert open(path, "rb").read() == payload
    n = len(calls)
    assert data._fetch("bookTicker", "BTCUSDT", day, False, lambda m: None) == path
    assert len(calls) == n  # cache hit: no download

    with pytest.raises(SystemExit, match="--offline"):
        data._fetch("aggTrades", "BTCUSDT", day, True, lambda m: None)
