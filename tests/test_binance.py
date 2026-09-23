"""Binance client tests. Every request goes to an in-memory transport double."""

import base64
import io
import json
import time
import urllib.error
import urllib.parse
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from stonkfly.binance import (
    NETWORKS,
    PUBLIC_DATA,
    BinanceClient,
    BinanceError,
    Ed25519Signer,
    HmacSigner,
    check,
    keygen,
    symbol,
)

# Example key and request from Binance's SIGNED endpoint documentation.
DOC_SECRET = "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0j"
DOC_QUERY = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559"
DOC_SIGNATURE = "c8db56825ae71d6d79447849e617115f4a920fa2acdcab2b053c4b2838bd6b71"


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Transport:
    """Routes requests by path to canned JSON; records everything sent."""

    def __init__(self, routes):
        self.routes = routes
        self.sent = []

    def __call__(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        self.sent.append(request)
        body = self.routes[url.path]
        if callable(body):
            body = body(dict(urllib.parse.parse_qsl(url.query)))
        if isinstance(body, urllib.error.HTTPError):
            raise body
        return Response(json.dumps(body).encode())


def http_error(status, code, msg):
    return urllib.error.HTTPError(
        "https://x",
        status,
        "err",
        {},
        io.BytesIO(json.dumps({"code": code, "msg": msg}).encode()),
    )


def test_hmac_matches_binance_documentation():
    assert HmacSigner(DOC_SECRET)(DOC_QUERY) == DOC_SIGNATURE


def test_ed25519_signature_verifies(tmp_path):
    key = Ed25519PrivateKey.generate()
    path = tmp_path / "k.pem"
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    sig = Ed25519Signer(path)(DOC_QUERY)
    key.public_key().verify(base64.b64decode(sig), DOC_QUERY.encode())


def test_signed_request_layout():
    t = Transport({"/api/v3/order": {"ok": True}})
    c = BinanceClient(
        NETWORKS["testnet"],
        "KEY",
        HmacSigner(DOC_SECRET),
        urlopen=t,
        clock=lambda: 1499827319.559,
    )
    c.signed(
        "POST",
        "/api/v3/order",
        symbol="LTCBTC",
        side="BUY",
        type="LIMIT",
        timeInForce="GTC",
        quantity=1,
        price=Decimal("0.1"),
    )
    (r,) = t.sent
    assert (
        r.full_url
        == f"{NETWORKS['testnet']}/api/v3/order?{DOC_QUERY}&signature={DOC_SIGNATURE}"
    )
    assert r.get_method() == "POST"
    assert r.get_header("X-mbx-apikey") == "KEY"
    assert DOC_SECRET not in r.full_url


def test_public_request_sends_no_key_or_signature():
    t = Transport({"/api/v3/depth": {"bids": [], "asks": []}})
    BinanceClient(PUBLIC_DATA, urlopen=t).get("/api/v3/depth", symbol="BTCUSDC")
    (r,) = t.sent
    assert "signature" not in r.full_url and "timestamp" not in r.full_url
    assert r.get_header("X-mbx-apikey") is None


def test_parameters_never_use_scientific_notation_or_float():
    t = Transport({"/x": {}})
    c = BinanceClient(PUBLIC_DATA, urlopen=t)
    c.get("/x", quantity=Decimal("1E-8"), symbols=["BTCUSDC", "ETHUSDC"])
    query = dict(
        urllib.parse.parse_qsl(urllib.parse.urlsplit(t.sent[0].full_url).query)
    )
    assert query == {"quantity": "0.00000001", "symbols": '["BTCUSDC","ETHUSDC"]'}
    with pytest.raises(TypeError):
        c.get("/x", price=0.1)
    with pytest.raises(ValueError):
        c.get("/x", price=Decimal("NaN"))


def test_http_error_is_raised_once_without_retry():
    t = Transport(
        {"/api/v3/order": http_error(400, -2010, "Order would immediately match")}
    )
    c = BinanceClient(NETWORKS["testnet"], "KEY", HmacSigner("s"), urlopen=t)
    with pytest.raises(BinanceError) as e:
        c.signed("POST", "/api/v3/order", symbol="BTCUSDC")
    assert e.value.code == -2010 and e.value.status == 400
    assert not e.value.outcome_unknown
    assert len(t.sent) == 1


def test_server_error_marks_outcome_unknown():
    t = Transport({"/api/v3/order": http_error(503, -1001, "Internal error")})
    c = BinanceClient(NETWORKS["testnet"], "KEY", HmacSigner("s"), urlopen=t)
    with pytest.raises(BinanceError) as e:
        c.signed("POST", "/api/v3/order", symbol="BTCUSDC")
    assert e.value.outcome_unknown
    assert len(t.sent) == 1


def test_numbers_parse_as_decimal():
    t = Transport({"/x": {"price": 0.1}})
    assert BinanceClient(PUBLIC_DATA, urlopen=t).get("/x")["price"] == Decimal("0.1")


def test_env_keys_are_separated_by_network(tmp_path):
    env = {"BINANCE_TESTNET_API_KEY": "t", "BINANCE_TESTNET_API_SECRET": "s"}
    assert BinanceClient.from_env("testnet", env).base == NETWORKS["testnet"]
    with pytest.raises(RuntimeError):
        BinanceClient.from_env("live", env)
    with pytest.raises(RuntimeError):
        BinanceClient.from_env(
            "testnet", {**env, "BINANCE_TESTNET_PRIVATE_KEY_FILE": str(tmp_path / "k")}
        )
    with pytest.raises(ValueError):
        BinanceClient.from_env("mainnet", env)


def test_client_configuration_bounds():
    with pytest.raises(ValueError):
        BinanceClient("https://example.com")
    with pytest.raises(ValueError):
        BinanceClient(PUBLIC_DATA, "KEY", HmacSigner("s"))
    with pytest.raises(ValueError):
        BinanceClient(NETWORKS["live"], "KEY")
    assert "s3cret" not in repr(HmacSigner("s3cret"))
    with pytest.raises(RuntimeError):
        BinanceClient(PUBLIC_DATA).signed("GET", "/api/v3/account")


def market(status="TRADING"):
    return {
        "symbols": [
            {
                "symbol": "BTCUSDC",
                "status": status,
                "baseAsset": "BTC",
                "quoteAsset": "USDC",
                "isSpotTradingAllowed": True,
                "orderTypes": ["LIMIT", "MARKET"],
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                    {
                        "filterType": "LOT_SIZE",
                        "stepSize": "0.00001",
                        "minQty": "0.00001",
                    },
                    {"filterType": "NOTIONAL", "minNotional": "5"},
                ],
            }
        ]
    }


def routes(**changes):
    r = {
        "/api/v3/time": lambda q: {"serverTime": int(time.time() * 1000)},
        "/api/v3/exchangeInfo": market(),
        "/api/v3/ticker/bookTicker": {"bidPrice": "100", "askPrice": "100.1"},
        "/api/v3/account": {
            "canTrade": True,
            "permissions": ["SPOT"],
            "balances": [
                {"asset": "USDC", "free": "100", "locked": "0"},
                {"asset": "BTC", "free": "0", "locked": "0"},
            ],
        },
        "/api/v3/account/commission": {
            "standardCommission": {"maker": "0.001", "taker": "0.001"}
        },
        "/api/v3/openOrders": [],
        "/sapi/v1/account/apiRestrictions": {
            "enableReading": True,
            "enableSpotAndMarginTrading": True,
            "enableWithdrawals": False,
            "enableInternalTransfer": False,
            "permitsUniversalTransfer": False,
            "ipRestrict": True,
        },
    }
    r.update(changes)
    return r


TESTNET_ENV = {"BINANCE_TESTNET_API_KEY": "t", "BINANCE_TESTNET_API_SECRET": "s"}
LIVE_ENV = {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}


def run_check(network, env, **changes):
    t = Transport(routes(**changes))
    report = check(network, ["BTC-USDC"], env, urlopen=t)
    return report, t


def test_check_is_read_only_and_testnet_skips_sapi():
    report, t = run_check("testnet", TESTNET_ENV)
    assert report["problems"] == []
    assert {r.get_method() for r in t.sent} == {"GET"}
    assert all(r.full_url.startswith(NETWORKS["testnet"]) for r in t.sent)
    assert not any("/sapi/" in r.full_url for r in t.sent)
    m = report["markets"]["BTCUSDC"]
    assert (m["tick_size"], m["step_size"], m["min_notional"]) == (
        "0.01",
        "0.00001",
        "5",
    )
    assert report["account"]["quote_balances"] == {
        "USDC": {"free": "100", "locked": "0"}
    }


def test_check_without_keys_reports_problem():
    report, t = run_check("testnet", {})
    assert "No account key configured" in report["problems"]
    assert not any(r.get_header("X-mbx-apikey") for r in t.sent)


def test_live_check_rejects_keys_that_can_move_funds():
    report, _ = run_check(
        "live",
        LIVE_ENV,
        **{
            "/sapi/v1/account/apiRestrictions": {
                "enableSpotAndMarginTrading": True,
                "enableWithdrawals": True,
            }
        },
    )
    assert "Key can move funds; disable withdrawals and transfers" in report["problems"]
    assert "Key is not IP-restricted" in report["warnings"]


def test_live_check_accepts_trade_only_key():
    report, t = run_check("live", LIVE_ENV)
    assert report["problems"] == [] and report["warnings"] == []
    assert all(r.full_url.startswith(NETWORKS["live"]) for r in t.sent)


def test_live_check_rejects_margin_or_futures_keys():
    restrictions = routes()["/sapi/v1/account/apiRestrictions"]
    for flag in ["enableMargin", "enableFutures"]:
        report, _ = run_check(
            "live",
            LIVE_ENV,
            **{"/sapi/v1/account/apiRestrictions": {**restrictions, flag: True}},
        )
        assert "Key allows margin loans or futures; spot only" in report["problems"]


def test_check_reports_missing_symbol_and_open_orders():
    report, _ = run_check(
        "testnet",
        TESTNET_ENV,
        **{"/api/v3/exchangeInfo": http_error(400, -1121, "Invalid symbol.")},
    )
    assert "BTCUSDC unavailable on testnet" in report["problems"]
    report, _ = run_check(
        "testnet", TESTNET_ENV, **{"/api/v3/openOrders": [{"orderId": 1}]}
    )
    assert "BTCUSDC has open orders" in report["problems"]


def test_symbol_mapping():
    assert symbol("BTC-USDC") == "BTCUSDC"


def test_keygen_writes_private_key_once(tmp_path):
    path = tmp_path / "k.pem"
    public = serialization.load_pem_public_key(keygen(path).encode())
    assert path.stat().st_mode & 0o777 == 0o600
    sig = Ed25519Signer(path)(DOC_QUERY)
    public.verify(base64.b64decode(sig), DOC_QUERY.encode())
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        keygen(path)
    assert path.read_bytes() == before
