"""Binance Spot client, paper observations, Spot Testnet execution and checks.

Spot Testnet is the default network; the real exchange must be named
explicitly. Requests are never retried here: a timeout or transport error
surfaces to the caller, so an order path can treat it as an unknown outcome.
"""

import base64
import hashlib
import hmac
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path

from .broker import UnresolvedOrder
from .config import D
from .market import CoinbaseMarket, Quote
from .risk import Veto

NETWORKS = {
    "testnet": "https://testnet.binance.vision",
    "live": "https://api.binance.com",
}
# Market data only, no account endpoints; paper observations need no key.
PUBLIC_DATA = "https://data-api.binance.vision"
ENV_PREFIX = {"testnet": "BINANCE_TESTNET_", "live": "BINANCE_"}


def symbol(product):
    """Stonkfly product name to Binance symbol: BTC-USDC -> BTCUSDC."""
    return product.replace("-", "")


class BinanceError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"Binance HTTP {status}, code {code}: {message}")
        self.status = status
        self.code = code
        # Binance documents 5XX as execution status UNKNOWN, never as failure.
        self.outcome_unknown = status >= 500


class HmacSigner:
    def __init__(self, secret):
        self._secret = secret.encode()

    def __call__(self, payload):
        return hmac.new(self._secret, payload.encode(), hashlib.sha256).hexdigest()

    def __repr__(self):
        return "HmacSigner(<redacted>)"


class Ed25519Signer:
    def __init__(self, path):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        key = load_pem_private_key(Path(path).read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError("Binance key file must be an unencrypted Ed25519 PEM")
        self._key = key

    def __call__(self, payload):
        return base64.b64encode(self._key.sign(payload.encode())).decode()

    def __repr__(self):
        return "Ed25519Signer(<redacted>)"


def keygen(path):
    """Create an Ed25519 private key file (mode 600); return the public PEM to register."""
    from cryptography.hazmat.primitives import serialization as s
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    private = key.private_bytes(s.Encoding.PEM, s.PrivateFormat.PKCS8, s.NoEncryption())
    # O_EXCL: never overwrite a key that may already be registered with Binance.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(private)
    return (
        key.public_key()
        .public_bytes(s.Encoding.PEM, s.PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def _param(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Nonfinite request quantity")
        # str(Decimal("1E-8")) is scientific notation, which Binance rejects.
        return format(value, "f")
    if isinstance(value, float):
        raise TypeError("Use Decimal or str for request quantities")
    if isinstance(value, (list, tuple)):
        return json.dumps(list(value), separators=(",", ":"))
    return str(value)


class BinanceClient:
    def __init__(
        self,
        base,
        api_key=None,
        signer=None,
        timeout=10,
        recv_window=5000,
        urlopen=urllib.request.urlopen,
        clock=time.time,
    ):
        if base not in (*NETWORKS.values(), PUBLIC_DATA):
            raise ValueError("Unknown Binance endpoint")
        if (api_key is None) != (signer is None):
            raise ValueError("API key and signer must be configured together")
        if base == PUBLIC_DATA and api_key is not None:
            raise ValueError("The public data endpoint takes no account key")
        if type(recv_window) is not int or not 0 < recv_window <= 60000:
            raise ValueError("recvWindow must be 1..60000 ms")
        self.base = base
        self._api_key = api_key
        self._signer = signer
        self.timeout = timeout
        self.recv_window = recv_window
        self._urlopen = urlopen
        self._clock = clock

    @classmethod
    def from_env(cls, network, environ=None, **kwargs):
        if network not in NETWORKS:
            raise ValueError("Binance network must be testnet or live")
        environ = os.environ if environ is None else environ
        prefix = ENV_PREFIX[network]
        key = environ.get(prefix + "API_KEY")
        pem = environ.get(prefix + "PRIVATE_KEY_FILE")
        secret = environ.get(prefix + "API_SECRET")
        if not key or bool(pem) == bool(secret):
            raise RuntimeError(
                f"Set {prefix}API_KEY and exactly one of "
                f"{prefix}PRIVATE_KEY_FILE or {prefix}API_SECRET locally"
            )
        signer = Ed25519Signer(pem) if pem else HmacSigner(secret)
        return cls(NETWORKS[network], key, signer, **kwargs)

    def __repr__(self):
        return f"BinanceClient({self.base!r}, signed={self._signer is not None})"

    def get(self, path, **params):
        return self._request("GET", path, params, signed=False)

    def signed(self, method, path, **params):
        if self._signer is None:
            raise RuntimeError("This Binance client has no account key")
        return self._request(method, path, params, signed=True)

    def clock_offset_ms(self):
        """Server minus local time. Signed requests fail beyond about 1 s ahead."""
        before = self._clock()
        server = self.get("/api/v3/time")["serverTime"]
        after = self._clock()
        return int(server - (before + after) * 500)

    def _request(self, method, path, params, signed):
        pairs = [(k, _param(v)) for k, v in params.items() if v is not None]
        headers = {"Accept": "application/json"}
        if signed:
            pairs += [
                ("recvWindow", str(self.recv_window)),
                ("timestamp", str(int(self._clock() * 1000))),
            ]
        query = urllib.parse.urlencode(pairs)
        if signed:
            signature = urllib.parse.quote(self._signer(query), safe="")
            query += ("&" if query else "") + "signature=" + signature
            headers["X-MBX-APIKEY"] = self._api_key
        data = None
        if method != "GET":
            data = b""
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        request = urllib.request.Request(
            self.base + path + ("?" + query if query else ""),
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read())
            except ValueError:
                err = {}
            # Binance error text never echoes the key; the URL is left out.
            raise BinanceError(
                e.code, err.get("code"), err.get("msg", "non-JSON error body")
            ) from None
        return json.loads(body, parse_float=Decimal)


def _filters(info):
    f = {x["filterType"]: x for x in info.get("filters", [])}
    notional = f.get("NOTIONAL") or f.get("MIN_NOTIONAL") or {}
    return {
        "tick_size": f.get("PRICE_FILTER", {}).get("tickSize"),
        "step_size": f.get("LOT_SIZE", {}).get("stepSize"),
        "min_qty": f.get("LOT_SIZE", {}).get("minQty"),
        "min_notional": notional.get("minNotional"),
    }


class BinanceMarket:
    """Public Binance observations for paper runs. Needs no account key."""

    def __init__(self, products, client=None, clock=time.time):
        self.client = client or BinanceClient(PUBLIC_DATA)
        self.products = products
        self.history = {p: [] for p in products}
        self._clock = clock

    def _seed(self, s):
        # Seed only completed, past one-minute candles. No future samples.
        end = int(self._clock() // 60) * 60 * 1000
        klines = self.client.get(
            "/api/v3/klines", symbol=s, interval="1m", endTime=end - 1, limit=120
        )
        past = sorted((k for k in klines if int(k[0]) < end), key=lambda k: int(k[0]))
        if not past:
            raise RuntimeError("No historical candles available")
        closes = [float(D(k[4])) for k in past]
        if any(not math.isfinite(v) or v <= 0 for v in closes):
            raise RuntimeError("Invalid historical price")
        return closes

    def snapshot(self):
        result = {}
        for product in self.products:
            s = symbol(product)
            if not self.history[product]:
                self.history[product] = self._seed(s)
            # Refresh tradeability at every observation, not just startup.
            info = self.client.get("/api/v3/exchangeInfo", symbol=s)["symbols"]
            m = info[0] if len(info) == 1 else {}
            if (
                m.get("symbol") != s
                or f"{m.get('baseAsset')}-{m.get('quoteAsset')}" != product
                or m.get("quoteAsset") != "USDC"
            ):
                raise RuntimeError("Unexpected product")
            if (
                m.get("status") != "TRADING"
                or m.get("isSpotTradingAllowed") is not True
                or "LIMIT" not in m.get("orderTypes", [])
            ):
                raise RuntimeError("Product unavailable for immediate spot execution")
            f = _filters(m)
            if None in f.values() or "quoteAssetPrecision" not in m:
                raise RuntimeError("Missing exchange filter")
            b = self.client.get("/api/v3/ticker/bookTicker", symbol=s)
            # Spot book responses carry no exchange timestamp; use receipt time.
            received = self._clock()
            if b.get("symbol") != s or not D(b["bidPrice"]) > 0:
                raise RuntimeError("Empty or mismatched book")
            result[product] = Quote(
                product,
                D(b["bidPrice"]),
                D(b["askPrice"]),
                received,
                D(f["step_size"]),
                D(1).scaleb(-int(m["quoteAssetPrecision"])),
                D(f["tick_size"]),
                D(f["min_notional"]),
                D(f["min_qty"]),
            )
        return result

    refresh = snapshot
    record = CoinbaseMarket.record


# Order states after which no further fill can occur.
FINAL = {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"}
# Documented error codes whose execution status is unknown despite a 4XX reply.
UNKNOWN_CODES = {-1006, -1007}


class BinanceBroker:
    """Spot Testnet execution with price-bounded fill-or-kill limit orders.

    Binance keys are account-wide, so the broker manages an allocation: it
    records the managed assets' balances at initialization and later requires
    each balance to equal that baseline plus the ledger's own fills. Intent is
    persisted before the request; an ambiguous result is never resent.
    """

    mode = "testnet"

    def __init__(self, settings, ledger, client):
        if client.base != NETWORKS["testnet"]:
            raise ValueError("Binance execution is implemented for Spot Testnet only")
        self.s = settings
        self.l = ledger
        self.client = client
        self.assets = ["USDC", *sorted({p.split("-")[0] for p in settings.products})]

    @classmethod
    def from_env(cls, settings, ledger):
        return cls(settings, ledger, BinanceClient.from_env("testnet"))

    def balances(self):
        a = self.client.signed("GET", "/api/v3/account")
        if a.get("canTrade") is not True:
            raise RuntimeError("Binance account cannot trade")
        rows = {b["asset"]: b for b in a.get("balances", [])}
        result = {}
        for asset in self.assets:
            b = rows.get(asset, {"free": "0", "locked": "0"})
            free, locked = D(b["free"]), D(b["locked"])
            if min(free, locked) < 0 or locked:
                raise RuntimeError("Negative or reserved balance in a managed asset")
            result[asset] = free
        return result

    def preflight(self):
        if abs(self.client.clock_offset_ms()) >= 1000:
            raise RuntimeError("Local clock differs from Binance by >= 1 s; sync it")
        self.reconcile()
        if self.l.get("binance_baseline") is None:
            if (
                self.l.get("tick")
                or self.l.db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
            ):
                raise RuntimeError("Uninitialized testnet ledger already has activity")
            balances = self.balances()
            if balances["USDC"] < D(self.s.capital):
                raise RuntimeError("Testnet USDC balance is below the allocation")
            with self.l.transaction():
                for k in ["cash", "initial_cash", "anchor"]:
                    self.l.put(k, self.s.capital)
                self.l.put("binance_baseline", {k: str(v) for k, v in balances.items()})
        self.verify_balances()
        return {
            "mode": self.mode,
            "allocation_usdc": self.s.capital,
            "managed_assets": self.assets,
        }

    def verify_balances(self):
        expected = {k: D(v) for k, v in self.l.get("binance_baseline").items()}
        expected["USDC"] += self.l.cash - D(self.l.get("initial_cash"))
        for p, amount in self.l.positions.items():
            expected[p.split("-")[0]] += amount
        actual = self.balances()
        if any(abs(actual[a] - expected[a]) > D(".00000001") for a in self.assets):
            raise RuntimeError(
                "External balance change; stop and reconcile rather than treat deposits as profit"
            )
        for product in self.s.products:
            if self.client.signed("GET", "/api/v3/openOrders", symbol=symbol(product)):
                raise RuntimeError("External/open order on a managed symbol")

    def execute(self, p, before_submit):
        cid = p["client_order_id"]
        order = {
            "symbol": symbol(p["product"]),
            "side": p["side"],
            "type": "LIMIT",
            "timeInForce": "FOK",
            "quantity": p["base_size"],
            "price": p["limit_price"],
            "newClientOrderId": cid,
        }
        try:
            try:
                test = self.client.signed(
                    "POST", "/api/v3/order/test", computeCommissionRates=True, **order
                )
            except BinanceError as e:
                raise Veto(f"Binance rejected the test order (code {e.code})") from None
            discount = test.get("discount", {})
            if discount.get("enabledForAccount") and discount.get("enabledForSymbol"):
                raise Veto("BNB fee payment is on; fees must stay in traded assets")
            rates = [
                (test.get(k) or {}).get("taker")
                for k in [
                    "standardCommissionForOrder",
                    "taxCommissionForOrder",
                    "specialCommissionForOrder",
                ]
            ]
            if rates[0] is None:
                raise Veto("Test order did not include fees")
            rate = sum((D(r) for r in rates if r is not None), D(0))
            fee = rate * D(p["base_size"]) * D(p["limit_price"])
            if rate < 0 or fee > D(p["fee_ceiling"]):
                raise Veto("Fee ceiling exceeded")
            if time.time() - p["quote_timestamp"] > self.s.max_quote_age:
                raise Veto("Quote expired during test order")
            self.verify_balances()
            before_submit(p)
        except Exception:
            self.l.mark(cid, "REJECTED")
            raise
        # This durable transition precedes any request that can place an order.
        self.l.mark(cid, "UNKNOWN")
        try:
            r = self.client.signed(
                "POST", "/api/v3/order", newOrderRespType="FULL", **order
            )
        except BinanceError as e:
            if (
                e.outcome_unknown
                or e.code in UNKNOWN_CODES
                or not 400 <= e.status < 500
            ):
                raise UnresolvedOrder(
                    "Submission outcome unknown; reconcile before any further trade"
                ) from None
            # A documented 4XX client error means Binance did not accept the order.
            self.l.mark(cid, "REJECTED")
            return {"mode": self.mode, "status": "REJECTED", "code": e.code}
        except Exception as e:
            raise UnresolvedOrder(
                "Submission outcome unknown; reconcile before any further trade"
            ) from e
        oid = r.get("orderId")
        if (
            oid is None
            or r.get("clientOrderId") != cid
            or r.get("symbol") != order["symbol"]
        ):
            raise UnresolvedOrder("Exchange response lacks an unambiguous order ID")
        self.l.mark(cid, "ACCEPTED", str(oid))
        deadline = time.monotonic() + 30
        while True:
            if self._settle(cid, str(oid), p):
                return {"mode": self.mode, "status": "SETTLED", "client_order_id": cid}
            if time.monotonic() >= deadline:
                break
            time.sleep(1)
        # FOK should be final at once. Do not assume that a timeout implies no fill.
        raise UnresolvedOrder("Order is not final; execution stopped")

    def _settle(self, cid, oid, p):
        s = symbol(p["product"])
        o = self.client.signed("GET", "/api/v3/order", symbol=s, orderId=oid)
        if (
            str(o.get("orderId")) != oid
            or o.get("clientOrderId") != cid
            or o.get("symbol") != s
            or o.get("side") != p["side"]
        ):
            raise UnresolvedOrder("Order identity mismatch")
        if o.get("status") not in FINAL:
            return False
        base, quote = D(o["executedQty"]), D(o["cummulativeQuoteQty"])
        fees = {}
        if base:
            trades = self.client.signed(
                "GET", "/api/v3/myTrades", symbol=s, orderId=oid
            )
            if any(str(t.get("orderId")) != oid for t in trades):
                raise UnresolvedOrder("Trade does not belong to this order")
            if (
                sum((D(t["qty"]) for t in trades), D(0)) != base
                or sum((D(t["quoteQty"]) for t in trades), D(0)) != quote
            ):
                return False  # Trade records have not caught up with the order.
            for t in trades:
                a = t["commissionAsset"]
                fees[a] = fees.get(a, D(0)) + D(t["commission"])
        fees = {a: v for a, v in fees.items() if v}
        base_asset = p["product"].split("-")[0]
        if len(fees) > 1 or not set(fees) <= {base_asset, "USDC"}:
            raise UnresolvedOrder(
                "Fee charged outside the traded assets; turn off BNB fee payment and reconcile manually"
            )
        asset, fee = next(iter(fees.items()), ("USDC", D(0)))
        self.l.settle(cid, base, quote, fee, "base" if asset == base_asset else "quote")
        return True

    def reconcile(self):
        for row in self.l.pending():
            cid = row["id"]
            p = row["plan"]
            oid = row["exchange_id"]
            if row["status"] == "PREPARED":
                # Network submission cannot have happened before UNKNOWN.
                self.l.mark(cid, "REJECTED")
                continue
            if not oid:
                try:
                    o = self.client.signed(
                        "GET",
                        "/api/v3/order",
                        symbol=symbol(p["product"]),
                        origClientOrderId=cid,
                    )
                except BinanceError as e:
                    # -2013 means not found; any other code leaves it unconfirmed.
                    raise UnresolvedOrder(
                        f"Uncertain submission not confirmed (code {e.code}). "
                        "Check Binance; no automatic resubmission."
                    ) from None
                if o.get("clientOrderId") != cid or o.get("orderId") is None:
                    raise UnresolvedOrder("Order lookup returned a different order")
                oid = str(o["orderId"])
                self.l.mark(cid, "ACCEPTED", oid)
            if not self._settle(cid, oid, p):
                raise UnresolvedOrder("Order not yet final at Binance")


def check(network, products, environ=None, urlopen=urllib.request.urlopen):
    """Read-only connectivity, market and account checks. Never submits orders."""
    if network not in NETWORKS:
        raise ValueError("Binance network must be testnet or live")
    public = BinanceClient(NETWORKS[network], urlopen=urlopen)
    report = {"network": network, "endpoint": NETWORKS[network], "read_only": True}
    problems, warnings = [], []
    offset = public.clock_offset_ms()
    report["clock_offset_ms"] = offset
    if abs(offset) >= 1000:
        problems.append("Local clock differs from Binance by >= 1 s; sync it")
    markets = {}
    for product in products:
        s = symbol(product)
        try:
            info = public.get("/api/v3/exchangeInfo", symbol=s)["symbols"][0]
        except BinanceError as e:
            markets[s] = {"error": str(e)}
            problems.append(f"{s} unavailable on {network}")
            continue
        book = public.get("/api/v3/ticker/bookTicker", symbol=s)
        bid, ask = Decimal(book["bidPrice"]), Decimal(book["askPrice"])
        markets[s] = {
            "status": info.get("status"),
            "base": info.get("baseAsset"),
            "quote": info.get("quoteAsset"),
            "limit_orders": "LIMIT" in info.get("orderTypes", []),
            **_filters(info),
            "bid": str(bid),
            "ask": str(ask),
            "spread": str((ask - bid) / bid) if bid > 0 else None,
        }
        if info.get("status") != "TRADING" or not info.get("isSpotTradingAllowed"):
            problems.append(f"{s} is not open for spot trading")
    report["markets"] = markets
    report["problems"] = problems
    report["warnings"] = warnings
    try:
        client = BinanceClient.from_env(network, environ, urlopen=urlopen)
    except RuntimeError as e:
        report["account"] = {"skipped": str(e)}
        problems.append("No account key configured")
        return report
    report["account"] = account = {"signer": type(client._signer).__name__}
    try:
        a = client.signed("GET", "/api/v3/account")
        balances = {b["asset"]: b for b in a.get("balances", [])}
        quotes = sorted({m["quote"] for m in markets.values() if "quote" in m})
        account["can_trade"] = a.get("canTrade")
        account["permissions"] = a.get("permissions")
        account["nonzero_assets"] = sum(
            1 for b in balances.values() if Decimal(b["free"]) or Decimal(b["locked"])
        )
        account["quote_balances"] = {
            q: {k: balances.get(q, {}).get(k, "0") for k in ["free", "locked"]}
            for q in quotes
        }
        if a.get("canTrade") is not True:
            problems.append("Account cannot trade")
        for s, m in markets.items():
            if "error" in m:
                continue
            fees = client.signed("GET", "/api/v3/account/commission", symbol=s)
            m["commission"] = fees.get("standardCommission")
            m["open_orders"] = len(client.signed("GET", "/api/v3/openOrders", symbol=s))
            if m["open_orders"]:
                problems.append(f"{s} has open orders")
    except BinanceError as e:
        account["error"] = str(e)
        problems.append("Signed account request failed")
    if network != "live":
        # /sapi endpoints exist only on the real exchange, not Spot Testnet.
        account["key_restrictions"] = "not available on Spot Testnet"
        return report
    try:
        r = client.signed("GET", "/sapi/v1/account/apiRestrictions")
    except BinanceError as e:
        account["key_restrictions"] = {"error": str(e)}
        problems.append("Could not read key permissions")
        return report
    account["key_restrictions"] = {
        k: r.get(k)
        for k in [
            "enableReading",
            "enableSpotAndMarginTrading",
            "enableWithdrawals",
            "enableInternalTransfer",
            "permitsUniversalTransfer",
            "enableMargin",
            "enableFutures",
            "ipRestrict",
        ]
    }
    if r.get("enableSpotAndMarginTrading") is not True:
        problems.append("Key lacks spot trading permission")
    # Missing fields fail closed: anything not explicitly False counts as enabled.
    if any(
        r.get(k) is not False
        for k in [
            "enableWithdrawals",
            "enableInternalTransfer",
            "permitsUniversalTransfer",
        ]
    ):
        problems.append("Key can move funds; disable withdrawals and transfers")
    if r.get("enableMargin") is True or r.get("enableFutures") is True:
        problems.append("Key allows margin loans or futures; spot only")
    if r.get("ipRestrict") is not True:
        warnings.append("Key is not IP-restricted")
    return report
