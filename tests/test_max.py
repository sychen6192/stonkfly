"""MAX paper observations. Every request goes to an in-memory transport double."""

import io
import json
import urllib.error
import urllib.parse
from decimal import Decimal

import pytest

from stonkfly import cli
from stonkfly.config import Settings
from stonkfly.ledger import Ledger
from stonkfly.max_exchange import BASE, MaxClient, MaxError, MaxMarket, market_id
from stonkfly.risk import Guard

# 30 s into a minute; the candle starting at MINUTE is still in progress.
MINUTE = 60 * 28_333_335
NOW = MINUTE + 30


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Transport:
    """Routes GETs by path to canned JSON; records everything sent."""

    def __init__(self, routes):
        self.routes = routes
        self.sent = []

    def __call__(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        self.sent.append(request)
        body = self.routes[url.path]
        if isinstance(body, urllib.error.HTTPError):
            raise body
        return Response(json.dumps(body).encode())


def btcusdt(**changes):
    return {
        "id": "btcusdt",
        "name": "BTC/USDT",
        "market_status": "active",
        "base_unit": "btc",
        "base_unit_precision": 6,
        "quote_unit": "usdt",
        "quote_unit_precision": 2,
        "min_base_amount": 0.00015,
        "min_quote_amount": 8,
        "m_wallet_supported": True,
        **changes,
    }


def routes(**changes):
    r = {
        "/api/v3/markets": [btcusdt(), {"id": "btctwd", "market_status": "active"}],
        "/api/v3/k": [
            [MINUTE, 1, 1, 1, 999.0, 1],  # in progress: must never be seen
            [MINUTE - 60, 1, 1, 1, 102.5, 1],
            [MINUTE - 180, 1, 1, 1, 100.0, 1],
            [MINUTE - 120, 1, 1, 1, 101.25, 1],
        ],
        # Deliberately unsorted: the adapter must not depend on level order.
        "/api/v3/depth": {
            "timestamp": MINUTE,
            "asks": [["100.30", "1"], ["100.10", "2"], ["100.20", "1"]],
            "bids": [["99.80", "1"], ["100.00", "3"], ["99.90", "1"]],
        },
    }
    r.update(changes)
    return r


def observe(**changes):
    t = Transport(routes(**changes))
    m = MaxMarket(["BTC-USDT"], MaxClient(urlopen=t), lambda: NOW)
    return m, m.snapshot()["BTC-USDT"], t


def test_market_id():
    assert market_id("BTC-USDT") == "btcusdt"


def test_seed_uses_start_time_and_only_completed_candles():
    m, _, t = observe()
    assert m.history["BTC-USDT"] == [100.0, 101.25, 102.5]
    (seed,) = [
        r for r in t.sent if urllib.parse.urlsplit(r.full_url).path == "/api/v3/k"
    ]
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(seed.full_url).query))
    assert q == {
        "market": "btcusdt",
        "period": "1",
        "limit": "120",
        "timestamp": str(MINUTE - 7200),
    }
    m.snapshot()
    assert sum("/api/v3/k" in r.full_url for r in t.sent) == 1


def test_quote_takes_best_levels_and_precision_increments():
    _, q, t = observe()
    assert (q.bid, q.ask, q.timestamp) == (Decimal("100.00"), Decimal("100.10"), NOW)
    assert q.price_increment == q.quote_increment == Decimal("0.01")
    assert q.base_increment == Decimal("0.000001")
    assert (q.minimum_base, q.minimum_quote) == (Decimal("0.00015"), Decimal("8"))
    assert all(r.full_url.startswith(BASE) and r.get_method() == "GET" for r in t.sent)
    assert not any(k.lower().startswith("x-max") for r in t.sent for k in r.headers)


@pytest.mark.parametrize(
    "changes",
    [
        {"/api/v3/markets": [btcusdt(market_status="suspended")]},
        {"/api/v3/markets": [btcusdt(quote_unit="twd")]},
        {"/api/v3/markets": [btcusdt(base_unit="eth")]},
        {"/api/v3/markets": []},
        {"/api/v3/depth": {"asks": [], "bids": [["1", "1"]]}},
        {"/api/v3/k": [[MINUTE, 1, 1, 1, 999.0, 1]]},
        {"/api/v3/k": [[MINUTE - 60, 1, 1, 1, 0, 1]]},
    ],
)
def test_market_fails_closed(changes):
    with pytest.raises(RuntimeError):
        observe(**changes)


def test_crossed_book_is_rejected():
    with pytest.raises(ValueError):
        observe(**{"/api/v3/depth": {"asks": [["99", "1"]], "bids": [["100", "1"]]}})


def test_http_error_carries_max_code_once():
    body = json.dumps({"error": {"code": 2002, "message": "market not found"}})
    err = urllib.error.HTTPError(BASE, 404, "nf", {}, io.BytesIO(body.encode()))
    t = Transport(routes(**{"/api/v3/markets": err}))
    with pytest.raises(MaxError) as e:
        MaxMarket(["BTC-USDT"], MaxClient(urlopen=t), lambda: NOW).snapshot()
    assert (e.value.status, e.value.code) == (404, 2002) and len(t.sent) == 1


def test_guard_plans_on_max_increments_in_usdt(tmp_path):
    _, q, _ = observe()
    s = Settings(products=("BTC-USDT",))
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "paper")
    try:
        g = Guard(s, ledger, tmp_path / "STOP")
        plan = g.plan("BTC-USDT", "BUY", {"BTC-USDT": q}, NOW)
    finally:
        ledger.close()
    size, limit = Decimal(plan["base_size"]), Decimal(plan["limit_price"])
    assert size % q.base_increment == 0 and limit % q.price_increment == 0
    assert size * limit >= q.minimum_quote


@pytest.mark.parametrize(
    "products",
    [("BTC-USDC", "BTC-USDT"), ("BTC-USDT", "BTC-USDT"), ("BTC-TWD",)],
)
def test_one_allowlisted_quote_asset_per_run(products):
    with pytest.raises(ValueError):
        Settings(products=products)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--exchange", "max", "--live"],
        ["run", "--exchange", "max", "--testnet"],
        ["run", "--exchange", "max", "--products", "BTC-USDC"],
        ["run", "--products", "BTC-USDT"],
        ["run", "--fixture", "--exchange", "max"],
    ],
)
def test_cli_keeps_max_paper_only_on_usdt(monkeypatch, tmp_path, argv):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["stonkfly", *argv])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    assert not (tmp_path / "runs").exists()
