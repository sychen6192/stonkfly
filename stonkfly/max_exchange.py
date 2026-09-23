"""Public MAX (MaiCoin) observations for paper runs. No account key, no orders.

MAX has no testnet, no fill-or-kill order type and no API-key permission
query, so this module only reads public market data; see docs/max.md.
Requests are never retried here.
"""

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from .config import D
from .market import CoinbaseMarket, Quote

BASE = "https://max-api.maicoin.com"


def market_id(product):
    """Stonkfly product name to MAX market ID: BTC-USDT -> btcusdt."""
    return product.replace("-", "").lower()


class MaxError(RuntimeError):
    def __init__(self, status, code, message):
        super().__init__(f"MAX HTTP {status}, code {code}: {message}")
        self.status = status
        self.code = code


class MaxClient:
    def __init__(self, timeout=10, urlopen=urllib.request.urlopen):
        self.timeout = timeout
        self._urlopen = urlopen

    def get(self, path, **params):
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        request = urllib.request.Request(
            BASE + path + ("?" + query if query else ""),
            headers={"Accept": "application/json"},
        )
        try:
            with self._urlopen(request, timeout=self.timeout) as response:
                body = response.read()
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read()).get("error")
            except (ValueError, AttributeError):
                err = None
            err = err if isinstance(err, dict) else {}
            raise MaxError(
                e.code, err.get("code"), err.get("message", "non-JSON error body")
            ) from None
        return json.loads(body, parse_float=Decimal)


class MaxMarket:
    """Public MAX observations for paper runs. Needs no account key."""

    def __init__(self, products, client=None, clock=time.time):
        self.client = client or MaxClient()
        self.products = products
        self.history = {p: [] for p in products}
        self._clock = clock

    def _seed(self, mid):
        # MAX queries candles by start time only. Keep completed, past minutes.
        end = int(self._clock() // 60) * 60
        rows = self.client.get(
            "/api/v3/k", market=mid, period=1, limit=120, timestamp=end - 120 * 60
        )
        past = sorted((k for k in rows if int(k[0]) < end), key=lambda k: int(k[0]))
        if not past:
            raise RuntimeError("No historical candles available")
        closes = [float(D(k[4])) for k in past][-120:]
        if any(not math.isfinite(v) or v <= 0 for v in closes):
            raise RuntimeError("Invalid historical price")
        return closes

    def snapshot(self):
        markets = {m.get("id"): m for m in self.client.get("/api/v3/markets")}
        result = {}
        for product in self.products:
            mid = market_id(product)
            base, quote = product.lower().split("-")
            if not self.history[product]:
                self.history[product] = self._seed(mid)
            # Refresh tradeability at every observation, not just startup.
            m = markets.get(mid, {})
            if (m.get("base_unit"), m.get("quote_unit")) != (base, quote) or (
                quote != "usdt"
            ):
                raise RuntimeError("Unexpected product")
            if m.get("status") != "active":
                raise RuntimeError("Product unavailable for spot trading")
            b = self.client.get("/api/v3/depth", market=mid, limit=5)
            # Level order is not guaranteed, so take the best prices directly.
            # As for Binance, receipt time stands in for the quote time.
            received = self._clock()
            bids = [D(level[0]) for level in b.get("bids", [])]
            asks = [D(level[0]) for level in b.get("asks", [])]
            if not bids or not asks:
                raise RuntimeError("Empty order book")
            tick = D(1).scaleb(-int(m["quote_unit_precision"]))
            result[product] = Quote(
                product,
                max(bids),
                min(asks),
                received,
                D(1).scaleb(-int(m["base_unit_precision"])),
                tick,
                tick,
                D(m["min_quote_amount"]),
                D(m["min_base_amount"]),
            )
        return result

    refresh = snapshot
    record = CoinbaseMarket.record
