import io
import json
import subprocess
import sys

import pytest

from backtest2live import mcp


def rpc(*msgs):
    out = io.StringIO()
    mcp.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def call(name, arguments, mid=2):
    return {"jsonrpc": "2.0", "id": mid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_handshake_and_tool_list():
    init, tools = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                       "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}}},
                      {"jsonrpc": "2.0", "method": "notifications/initialized"},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert init["result"]["protocolVersion"] == "2025-06-18"
    assert init["result"]["capabilities"] == {"tools": {}}
    assert [t["name"] for t in tools["result"]["tools"]] == ["convergence", "fill_gap", "cost_gap"]


def test_unknown_protocol_version_gets_ours():
    (r,) = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "1999-01-01"}})
    assert r["result"]["protocolVersion"] == mcp.PROTOCOL_VERSIONS[0]


def test_convergence_returns_report_and_json(root):
    (r,) = rpc(call("convergence", {"path": str(root / "examples/results_converged.csv"), "metric": "sharpe_oos"}))
    res = r["result"]
    assert res["isError"] is False
    md, js = res["content"]
    assert "## Verdict: CONVERGED" in md["text"]
    assert json.loads(js["text"]) == res["structuredContent"]
    assert res["structuredContent"]["verdict"] == "CONVERGED"


def test_bad_input_is_a_tool_error_not_a_crash(root):
    a, b, c = rpc(call("convergence", {"path": "/nonexistent.csv"}, 1),
                  call("convergence", {"path": "x.csv", "bogus": 1}, 2),
                  call("convergence", {"path": str(root / "examples/results_converged.csv"), "top_k": 0}, 3))
    assert a["result"]["isError"] and "file not found" in a["result"]["content"][0]["text"]
    assert b["result"]["isError"] and "unknown argument" in b["result"]["content"][0]["text"]
    assert c["result"]["isError"] and "--top-k" in c["result"]["content"][0]["text"]


def test_unknown_tool_and_method():
    a, b = rpc(call("nope", {}, 1), {"jsonrpc": "2.0", "id": 2, "method": "resources/list"})
    assert a["error"]["code"] == -32602 and b["error"]["code"] == -32601


def test_parse_error_answers_and_keeps_serving():
    out = io.StringIO()
    mcp.serve(io.StringIO("not json\n" + json.dumps({"jsonrpc": "2.0", "id": 5, "method": "ping"}) + "\n"), out)
    first, second = [json.loads(x) for x in out.getvalue().splitlines()]
    assert first["error"]["code"] == -32700 and second == {"jsonrpc": "2.0", "id": 5, "result": {}}


def test_argv_mapping():
    assert mcp._argv("fill_gap", {"orders": "o.csv", "venue": "binance-um", "pair": "BTCUSDT", "offline": True,
                                  "l2": ["a", "b"], "latency_ms": 5}) == \
        ["fill-gap", "o.csv", "--venue", "binance-um", "--pair", "BTCUSDT", "--offline", "--l2", "a", "b",
         "--latency-ms", "5"]


def test_cost_gap_over_the_protocol(write_csv):
    pytest.importorskip("pandas")
    from conftest import T
    book = write_csv(f"ts,bid_px,bid_qty,ask_px,ask_qty\n{T + 1000},100.0,5,100.1,2\n{T + 9000},100.0,5,100.1,2", "b.csv")
    orders = write_csv(f"ts,side,size,fill_price\n{T + 2000},buy,1,100.05", "o.csv")
    (r,) = rpc(call("cost_gap", {"orders": orders, "book": book}))
    assert r["result"]["isError"] is False and r["result"]["structuredContent"]["verdict"] == "REPORTED"


def test_stdout_carries_only_protocol(root):
    """End to end through the CLI: evaluator logs go to stderr, stdout is one JSON-RPC line per answer."""
    msgs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            call("convergence", {"path": str(root / "examples/results_converged.csv")})]
    p = subprocess.run([sys.executable, "-m", "backtest2live", "mcp"], input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, cwd=root, timeout=60)
    lines = p.stdout.splitlines()
    assert p.returncode == 0 and len(lines) == 2
    assert json.loads(lines[1])["result"]["structuredContent"]["verdict"] == "CONVERGED"


def test_non_finite_numbers_become_null(monkeypatch):
    doc = {"a": float("nan"), "b": [1.0, float("inf")], "c": {"d": float("-inf")}}
    monkeypatch.setattr(mcp, "call", lambda tool, args: ("md", doc))
    out = io.StringIO()
    mcp.serve(io.StringIO(json.dumps(call("convergence", {})) + "\n"), out)
    line = out.getvalue()
    assert "NaN" not in line and "Infinity" not in line
    res = json.loads(line)["result"]
    assert res["structuredContent"] == {"a": None, "b": [1.0, None], "c": {"d": None}}
