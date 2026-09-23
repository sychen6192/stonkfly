"""Read-only monitor: page data from a run directory and a local HTTP server."""

import json
import os
import threading
import urllib.error
import urllib.request
from decimal import Decimal as D

import pytest

from stonkfly.config import Settings
from stonkfly.ledger import Ledger
from stonkfly.monitor import runs, server, state

PNG = b"\x89PNG\r\n\x1a\nfly view"
MINUTE = 1790000040.0  # fixed clock; all activity below falls on one UTC day


def event(tick, side, bid, execution, **neural):
    """One events.jsonl row, shaped like the run loop writes it."""
    return {
        "tick": tick,
        "wall_time": MINUTE + 60 * tick,
        "product": "BTC-USDC",
        "mode": "paper",
        "quote": {
            "product": "BTC-USDC",
            "bid": bid,
            "ask": str(D(bid) + D("0.01")),
            "timestamp": MINUTE + 60 * tick - 1,
            "base_increment": "0.00001",
            "quote_increment": "1E-8",
            "price_increment": "0.01",
            "minimum_quote": "5",
            "minimum_base": "0.00001",
        },
        "equity_usdc": "100",
        "pnl_delta_usdc": "0",
        "neural": {
            "side": side,
            "left_hz": 30.0,
            "right_hz": 40.0,
            "difference_hz": 10.0,
            "gate_spikes": 3,
            "cell_ids": {"left": ["10162"], "right": ["10059"], "gate": ["10527"]},
            "brain_ms": 500.0 * tick,
            "compute_seconds": 1.2,
            "stimulus": "none",
            "stimulus_ms": 0.0,
            "reward_spikes": 0,
            "aversive_spikes": 5,
            "KC_spikes": 12,
            "total_spikes": 380000,
            "spike_sha256": "s",
            "input_sha256": "i",
            "memory": {
                "plastic_edges": 7835,
                "changed_edges": 5,
                "mean_efficacy": 1.0,
                "minimum_efficacy": 0.99,
                "sha256": "m",
                "model": "stonkfly-dual-compartment-v1",
            },
            **neural,
        },
        "execution": execution,
    }


def traded_run(root, name="paper"):
    """A run that bought 0.0001 BTC for 8 USDC plus a 0.048 fee, then vetoed."""
    run = root / name
    ledger = Ledger(run / "ledger.sqlite", Settings(), "paper")
    plan = {
        "product": "BTC-USDC",
        "side": "BUY",
        "base_size": "0.0001",
        "limit_price": "80400",
        "fee_ceiling": "0.2",
    }
    cid = ledger.reserve(plan, MINUTE + 60)["client_order_id"]
    ledger.settle(cid, "0.0001", "8", "0.048")
    ledger.commit_tick("100", None)
    ledger.commit_tick("100.052", None)
    ledger.close()
    rows = [
        event(1, "BUY", "80000", {"status": "FILLED", "mode": "paper"}),
        event(
            2,
            "BUY",
            "81000",
            {"status": "VETO", "reason": "Order cooldown"},
            gate_spikes=0,
        ),
    ]
    (run / "events.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (run / "latest-input.png").write_bytes(PNG)
    return run


def test_account_is_marked_at_the_latest_bid(tmp_path):
    s = state(traded_run(tmp_path), now=MINUTE + 100)
    a = s["account"]
    assert D(a["cash"]) == D("91.952")  # 100 - 8 - 0.048
    assert a["positions"] == {"BTC-USDC": "0.0001"}
    assert D(a["value"]) == D("100.052")  # 91.952 + 0.0001 * 81000
    assert D(a["pnl"]) == D("0.052")


def test_decisions_orders_and_limits(tmp_path):
    s = state(traded_run(tmp_path), now=MINUTE + 100)
    assert [(e["tick"], e["side"], e["execution"]) for e in s["history"]] == [
        (1, "BUY", "FILLED"),
        (2, "BUY", "VETO"),
    ]
    last = s["history"][-1]
    assert (last["reason"], last["gate_spikes"], last["bid"]) == (
        "Order cooldown",
        0,
        "81000",
    )
    assert s["latest"]["neural"]["cell_ids"]["gate"] == ["10527"]
    (order,) = s["orders"]
    assert (order["side"], order["size"], order["quote"], order["fee"]) == (
        "BUY",
        "0.0001",
        "8",
        "0.048",
    )
    assert order["status"] == "SETTLED"
    # Reserved at MINUTE + 60 with a 60 s interval: 20 s of cooldown remain.
    assert (s["attempts_today"], s["daily_orders"], s["cooldown_seconds"]) == (
        1,
        24,
        20,
    )
    assert s["image"] is True


def test_run_before_its_first_observation(tmp_path):
    run = tmp_path / "fresh"
    Ledger(run / "ledger.sqlite", Settings(), "paper").close()
    s = state(run)
    assert (s["history"], s["latest"], s["orders"], s["image"]) == ([], None, [], False)
    assert D(s["account"]["value"]) == 100


def test_history_skips_a_line_still_being_written(tmp_path):
    run = traded_run(tmp_path)
    with (run / "events.jsonl").open("a") as f:
        f.write('{"tick": 3, "wall_ti')
    assert [e["tick"] for e in state(run)["history"]] == [1, 2]


def test_reports_why_a_run_stopped(tmp_path):
    run = tmp_path / "halted"
    ledger = Ledger(run / "ledger.sqlite", Settings(), "paper")
    ledger.halt("Loss stop reached; holdings remain exposed")
    ledger.close()
    (run / "STOP").touch()
    (run / "error.json").write_text(json.dumps({"type": "Veto", "reason": "Loss stop"}))
    s = state(run)
    assert s["halted"] == "Loss stop reached; holdings remain exposed"
    assert s["stop_file"] is True and s["error"]["reason"] == "Loss stop"


def test_reading_leaves_the_run_untouched(tmp_path):
    run = traded_run(tmp_path)

    def snapshot():
        # SQLite may add its own empty -wal/-shm index files for a reader.
        return {
            p.name: p.read_bytes()
            for p in run.iterdir()
            if not p.name.endswith(("-wal", "-shm"))
        }

    before = snapshot()
    state(run)
    assert snapshot() == before


def test_runs_lists_ledgers_newest_first(tmp_path):
    old = traded_run(tmp_path, "old")
    traded_run(tmp_path, "new")
    (tmp_path / "not-a-run").mkdir()
    for path in old.iterdir():
        path.touch()  # most recent activity is now in "old"
    assert [p.name for p in runs(tmp_path)] == ["old", "new"]


def test_reading_a_run_does_not_make_it_the_newest(tmp_path):
    old = traded_run(tmp_path, "old")
    new = traded_run(tmp_path, "new")
    for run, stamp in [(old, 1_000_000), (new, 2_000_000)]:
        for path in run.iterdir():
            os.utime(path, (stamp, stamp))
    state(old)  # SQLite may create or touch -wal/-shm files for a reader
    assert [p.name for p in runs(tmp_path)] == ["new", "old"]


@pytest.fixture
def http(tmp_path):
    traded_run(tmp_path, "paper")
    (tmp_path / "secret.txt").write_text("outside any run")
    httpd = server(tmp_path, port=0)
    threading.Thread(
        target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    ).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(path):
        try:
            with urllib.request.urlopen(base + path, timeout=5) as r:
                return r.status, r.headers["Content-Type"], r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers["Content-Type"], e.read()

    yield get
    httpd.shutdown()
    httpd.server_close()


def test_serves_page_state_and_image(http):
    status, kind, _ = http("/")
    assert status == 200 and kind.startswith("text/html")
    status, _, body = http("/api/runs")
    assert json.loads(body) == ["paper"]
    status, kind, body = http("/api/state?run=paper")
    assert status == 200 and json.loads(body)["tick"] == 2
    assert http("/image?run=paper") == (200, "image/png", PNG)


@pytest.mark.parametrize(
    "path",
    [
        "/api/state?run=..",
        "/api/state?run=../paper",
        "/api/state?run=nope",
        "/image?run=secret.txt",
        "/secret.txt",
        "/api/state",
    ],
)
def test_nothing_outside_listed_runs_is_served(http, path):
    assert http(path)[0] == 404


def test_unreadable_state_is_an_error_not_a_stale_page(http, tmp_path):
    with (tmp_path / "paper/events.jsonl").open("a") as f:
        f.write('{"tick": 3, "wall_ti\n')  # a complete line that is not JSON
    status, _, body = http("/api/state?run=paper")
    assert status == 500 and b"JSONDecodeError" in body


def test_listens_only_on_this_computer(tmp_path):
    httpd = server(tmp_path, port=0)
    try:
        assert httpd.server_address[0] == "127.0.0.1"
    finally:
        httpd.server_close()
