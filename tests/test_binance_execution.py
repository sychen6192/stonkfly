"""Spot Testnet execution tests. Every request goes to an in-memory exchange."""

import dataclasses
import io
import json
import time
import urllib.error
import urllib.parse
from decimal import Decimal

import pytest

from stonkfly import cli
from stonkfly.actions import StonkflyActions
from stonkfly.binance import NETWORKS, BinanceBroker, BinanceClient, HmacSigner
from stonkfly.broker import UnresolvedOrder
from stonkfly.config import D, Settings
from stonkfly.ledger import Ledger
from stonkfly.market import Quote
from stonkfly.risk import Guard, Veto

EIGHT = Decimal("0.00000001")


def quote(**changes):
    q = Quote(
        "BTC-USDC",
        D("100"),
        D("100.1"),
        time.time(),
        D(".00001"),
        EIGHT,
        D(".01"),
        D("5"),
        D(".00001"),
    )
    return dataclasses.replace(q, **changes)


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(status, code, msg="error"):
    body = json.dumps({"code": code, "msg": msg}).encode()
    return urllib.error.HTTPError("https://x", status, "err", {}, io.BytesIO(body))


class Testnet:
    """Models documented Spot endpoints; FOK orders match at the fixed book."""

    __test__ = False

    def __init__(self):
        self.balances = {"USDC": D("5000"), "BTC": D("1"), "BNB": D("2")}
        self.locked = {}
        self.orders = {}
        self.trades = {}
        self.submissions = []
        self.open_orders = []
        self.fill = True
        self.rate = "0.001"
        self.commission_asset = None
        self.discount = False
        self.discount_asset = "BNB"
        self.discount_rate = "0.25"
        self.test_error = None
        self.after_test = lambda: None
        self.reject = None  # HTTPError raised instead of placing the order
        self.lose_response = False  # order placed, reply lost
        self.clock_offset_ms = 0

    def __call__(self, request, timeout):
        url = urllib.parse.urlsplit(request.full_url)
        assert request.full_url.startswith(NETWORKS["testnet"])
        q = dict(urllib.parse.parse_qsl(url.query))
        route = (request.get_method(), url.path)
        if url.path != "/api/v3/time":
            assert request.get_header("X-mbx-apikey") == "KEY" and "signature" in q
        handler = {
            ("GET", "/api/v3/time"): self.time,
            ("GET", "/api/v3/account"): self.account,
            ("GET", "/api/v3/openOrders"): lambda q: self.open_orders,
            ("POST", "/api/v3/order/test"): self.test_order,
            ("POST", "/api/v3/order"): self.new_order,
            ("GET", "/api/v3/order"): self.get_order,
            ("GET", "/api/v3/myTrades"): lambda q: self.trades[int(q["orderId"])],
        }[route]
        return Response(json.dumps(handler(q)).encode())

    def time(self, q):
        return {"serverTime": int(time.time() * 1000) + self.clock_offset_ms}

    def account(self, q):
        return {
            "canTrade": True,
            "balances": [
                {"asset": a, "free": str(v), "locked": str(self.locked.get(a, 0))}
                for a, v in self.balances.items()
            ],
        }

    def test_order(self, q):
        assert q["computeCommissionRates"] == "true"
        if self.test_error:
            raise self.test_error
        self.after_test()
        rates = {"maker": self.rate, "taker": self.rate}
        return {
            "standardCommissionForOrder": rates,
            "taxCommissionForOrder": {"maker": "0", "taker": "0"},
            "discount": {
                "enabledForAccount": self.discount,
                "enabledForSymbol": True,
                "discountAsset": self.discount_asset,
                "discount": self.discount_rate,
            },
        }

    def new_order(self, q):
        self.submissions.append(q)
        if self.reject:
            raise self.reject
        assert (q["type"], q["timeInForce"], q["newOrderRespType"]) == (
            "LIMIT",
            "FOK",
            "FULL",
        )
        side, qty, limit = q["side"], D(q["quantity"]), D(q["price"])
        price = D("100.1") if side == "BUY" else D("100")
        crosses = limit >= price if side == "BUY" else limit <= price
        oid = len(self.orders) + 1
        trades = []
        if self.fill and crosses:
            value = (qty * price).quantize(EIGHT)
            asset = self.commission_asset or ("BTC" if side == "BUY" else "USDC")
            fee = ((qty if asset == "BTC" else value) * D(self.rate)).quantize(EIGHT)
            if side == "BUY":
                self.balances["USDC"] -= value
                self.balances["BTC"] += qty
            else:
                self.balances["BTC"] -= qty
                self.balances["USDC"] += value
            self.balances[asset] -= fee
            trades.append(
                {
                    "orderId": oid,
                    "qty": str(qty),
                    "quoteQty": str(value),
                    "commission": str(fee),
                    "commissionAsset": asset,
                }
            )
        self.trades[oid] = trades
        filled = sum((D(t["qty"]) for t in trades), D(0))
        self.orders[oid] = {
            "symbol": q["symbol"],
            "orderId": oid,
            "clientOrderId": q["newClientOrderId"],
            "side": side,
            "status": "FILLED" if trades else "EXPIRED",
            "executedQty": str(filled),
            "cummulativeQuoteQty": str(sum((D(t["quoteQty"]) for t in trades), D(0))),
        }
        if self.lose_response:
            raise TimeoutError("reply lost after the order was placed")
        return self.orders[oid]

    def get_order(self, q):
        for o in self.orders.values():
            if str(o["orderId"]) == q.get("orderId") or o["clientOrderId"] == q.get(
                "origClientOrderId"
            ):
                return o
        raise http_error(400, -2013, "Order does not exist.")


@pytest.fixture
def env(tmp_path):
    s = Settings(paper_fee="0.001")
    ledger = Ledger(tmp_path / "ledger.sqlite", s, "testnet")
    guard = Guard(s, ledger, tmp_path / "STOP")
    yield s, ledger, guard
    ledger.close()


def make_broker(env, preflight=True):
    s, l, g = env
    ex = Testnet()
    client = BinanceClient(NETWORKS["testnet"], "KEY", HmacSigner("s"), urlopen=ex)
    broker = BinanceBroker(s, l, client)
    if preflight:
        broker.preflight()
    provider = StonkflyActions(g, broker)
    provider.quotes = {"BTC-USDC": quote()}
    return ex, broker, provider


def buy(provider):
    return provider.get_actions()[0].invoke({"product": "BTC-USDC", "side": "BUY"})


def test_preflight_allocates_from_testnet_balances(env):
    ex, broker, _ = make_broker(env)
    _, l, _ = env
    assert l.cash == D("100") and l.get("initial_cash") == "100"
    assert l.get("binance_baseline") == {"USDC": "5000", "BTC": "1"}
    assert broker.assets == ["USDC", "BTC"]  # BNB is never managed


@pytest.mark.parametrize("change", ["underfunded", "locked", "open", "clock", "used"])
def test_preflight_refuses_unsafe_start(env, change):
    s, l, g = env
    ex, broker, _ = make_broker(env, preflight=False)
    if change == "underfunded":
        ex.balances["USDC"] = D("99")
    if change == "locked":
        ex.locked["BTC"] = D("0.1")
    if change == "open":
        ex.open_orders = [{"orderId": 9}]
    if change == "clock":
        ex.clock_offset_ms = 5000
    if change == "used":
        l.put("tick", 3)
    with pytest.raises(RuntimeError):
        broker.preflight()
    assert not ex.submissions


def test_buy_fok_settles_with_fee_in_base(env):
    ex, broker, provider = make_broker(env)
    _, l, _ = env
    assert buy(provider)["status"] == "SETTLED"
    (sent,) = ex.submissions
    (row,) = l.db.execute("SELECT id, plan FROM orders").fetchall()
    plan = json.loads(row[1])
    assert sent["newClientOrderId"] == row[0]
    assert (sent["quantity"], sent["price"]) == (plan["base_size"], plan["limit_price"])
    (trade,) = ex.trades[1]
    assert l.positions["BTC-USDC"] == D(trade["qty"]) - D(trade["commission"])
    assert l.cash == D("100") - D(trade["quoteQty"])
    broker.verify_balances()


def test_sell_settles_with_fee_in_quote(env):
    ex, broker, provider = make_broker(env)
    _, l, _ = env
    buy(provider)
    l.put("last_attempt", 0)
    cash, held = l.cash, l.positions["BTC-USDC"]
    r = provider.get_actions()[0].invoke({"product": "BTC-USDC", "side": "SELL"})
    assert r["status"] == "SETTLED"
    (trade,) = ex.trades[2]
    assert trade["commissionAsset"] == "USDC"
    assert l.cash == cash + D(trade["quoteQty"]) - D(trade["commission"])
    assert l.positions["BTC-USDC"] == held - D(trade["qty"])
    broker.verify_balances()


def test_unfilled_fok_books_nothing(env):
    ex, broker, provider = make_broker(env)
    ex.fill = False
    assert buy(provider)["status"] == "SETTLED"
    assert env[1].cash == D("100") and not env[1].pending()
    broker.verify_balances()


def test_lost_reply_reconciles_without_resubmission(env):
    ex, broker, provider = make_broker(env)
    _, l, _ = env
    ex.lose_response = True
    with pytest.raises(UnresolvedOrder):
        buy(provider)
    assert l.pending()[0]["status"] == "UNKNOWN"
    with pytest.raises(Veto):
        buy(provider)
    broker.reconcile()
    broker.reconcile()
    broker.verify_balances()
    assert len(ex.submissions) == 1 and not l.pending()


@pytest.mark.parametrize("status,code", [(503, -1001), (408, -1007), (400, -1006)])
def test_unknown_submission_stays_stopped(env, status, code):
    ex, broker, provider = make_broker(env)
    ex.reject = http_error(status, code)
    with pytest.raises(UnresolvedOrder):
        buy(provider)
    with pytest.raises(UnresolvedOrder, match="no automatic resubmission"):
        broker.reconcile()
    assert len(ex.submissions) == 1
    assert env[1].pending()[0]["status"] == "UNKNOWN"


def test_documented_client_rejection_is_final(env):
    ex, broker, provider = make_broker(env)
    ex.reject = http_error(400, -2010, "Account has insufficient balance")
    assert buy(provider)["status"] == "REJECTED"
    assert not env[1].pending() and env[1].cash == D("100")


@pytest.mark.parametrize("failure", ["test_error", "bnb", "fee", "stop", "stale"])
def test_checks_before_submission_never_send(env, failure):
    ex, broker, provider = make_broker(env)
    if failure == "test_error":
        ex.test_error = http_error(400, -1013, "Filter failure: NOTIONAL")
    if failure == "bnb":
        ex.discount = True
    if failure == "fee":
        ex.rate = "0.05"
    if failure == "stop":
        ex.after_test = env[2].stop_file.touch
    if failure == "stale":
        provider.quotes = {"BTC-USDC": quote(timestamp=time.time() - 20)}
    with pytest.raises(Veto):
        buy(provider)
    assert not ex.submissions and not env[1].pending()


def test_zero_fee_order_trades_despite_discount_flags(env):
    # Live Spot Testnet, 2026-09-24: every rate is 0 while both discount flags
    # stay on without a discount asset. No fee can be charged in any asset.
    ex, broker, provider = make_broker(env)
    ex.rate = "0.00000000"
    ex.discount, ex.discount_asset, ex.discount_rate = True, None, "0.00000000"
    ex.commission_asset = "BNB"
    assert buy(provider)["status"] == "SETTLED"
    (trade,) = ex.trades[1]
    assert env[1].positions["BTC-USDC"] == D(trade["qty"])
    assert env[1].cash == D("100") - D(trade["quoteQty"])
    broker.verify_balances()


def test_fee_outside_traded_assets_stops_before_booking(env):
    ex, broker, provider = make_broker(env)
    ex.commission_asset = "BNB"
    with pytest.raises(UnresolvedOrder, match="BNB"):
        buy(provider)
    assert env[1].pending()[0]["status"] == "ACCEPTED"
    assert env[1].cash == D("100")


def test_external_balance_change_stops(env):
    ex, broker, _ = make_broker(env)
    ex.balances["USDC"] += 1
    with pytest.raises(RuntimeError, match="External balance change"):
        broker.verify_balances()


def test_order_identity_mismatch_is_unresolved(env):
    ex, broker, provider = make_broker(env)
    ex.lose_response = True
    with pytest.raises(UnresolvedOrder):
        buy(provider)
    ex.orders[1]["side"] = "SELL"
    with pytest.raises(UnresolvedOrder, match="identity"):
        broker.reconcile()


def test_prepared_intent_is_rejected_on_reconcile(env):
    ex, broker, _ = make_broker(env)
    s, l, g = env
    l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    broker.reconcile()
    assert not l.pending() and not ex.submissions


def test_broker_is_testnet_only(env):
    s, l, _ = env
    live = BinanceClient(NETWORKS["live"], "KEY", HmacSigner("s"))
    with pytest.raises(ValueError, match="Testnet"):
        BinanceBroker(s, l, live)


def test_base_fee_counts_against_quote_fee_ceiling(env):
    _, l, g = env
    p = l.reserve(g.plan("BTC-USDC", "BUY", {"BTC-USDC": quote()}), time.time())
    size = D(p["base_size"])
    ceiling_in_base = D(p["fee_ceiling"]) / D(p["limit_price"])
    with pytest.raises(ValueError):
        l.settle(p["client_order_id"], size, size * 100, 0, "BNB")
    l.settle(p["client_order_id"], size, size * 100, ceiling_in_base * 2, "base")
    assert "fee exceeded" in l.get("halted")
    assert l.positions["BTC-USDC"] == size - ceiling_in_base * 2


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--testnet"],
        ["run", "--testnet", "--exchange", "binance", "--live"],
        ["run", "--testnet", "--exchange", "binance", "--fixture"],
    ],
)
def test_cli_testnet_needs_binance_and_excludes_live(monkeypatch, tmp_path, argv):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["stonkfly", *argv])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 2
    assert not (tmp_path / "runs").exists()
