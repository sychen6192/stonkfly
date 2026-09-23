"""Binance Spot REST client, public paper observations and a read-only check.

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

from .config import D
from .market import CoinbaseMarket, Quote

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

    record = CoinbaseMarket.record


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
