# MAX (MaiCoin) paper trading

Status: **paper trading on public MAX prices only.** `stonkfly/max_exchange.py` reads public market data; it needs no key and cannot place an order. `run --exchange max` refuses `--live` and `--testnet`.

```sh
python -m stonkfly run --exchange max
python -m stonkfly status --out runs/max-paper
```

Runs trade **USDT pairs** (`BTC-USDT` by default; `ETH-USDT` and `SOL-USDT` via `--products`) with the same 100-unit capital, 10-unit order and 20-unit drawdown limits, counted in USDT. TWD markets are excluded because those limits are denominated in the quote asset. Paper fills charge 0.16%, MAX's VIP0 taker fee; your tier can differ. The chart, neural model, decoder and limits are otherwise unchanged. Runs default to `runs/max-paper`.

## Observations

All requests go to `https://max-api.maicoin.com`:

- `GET /api/v3/k` seeds the chart with completed one-minute closes. MAX queries candles by start time only, so the adapter asks for 120 minutes before the current minute and drops the one still in progress.
- `GET /api/v3/markets` is re-read at every observation. The market must be `active` and have the expected base and quote units. Tick and step sizes come from `quote_unit_precision` and `base_unit_precision`; minimums come from `min_quote_amount` and `min_base_amount`.
- `GET /api/v3/depth` gives the book. Level order is not guaranteed, so the adapter takes the highest bid and the lowest ask. As with Binance, local receipt time stands in for the quote time.

Anything missing, inactive or crossed stops the run instead of guessing. These formats follow the MAX v3 API as implemented by the open-source bbgo MAX adapter. They are not yet verified against the live API from this repository's development environment, which cannot reach MAX.

Caveats:

- If a market's `min_quote_amount` is above what a 10 USDT order can buy after the 2% fee reserve, every order is vetoed as below the exchange minimum.
- Thinner USDT books can exceed the 0.5% spread limit, which vetoes that observation.

## Why there are no MAX orders

A MAX order path could not keep this repository's execution guarantees without changes:

- **No testnet.** The first order anywhere would use real funds.
- **No fill-or-kill.** The closest type, `ioc_limit`, can partially fill.
- **No order preview or fee quote.** Fee ceilings would rely on a locally assumed rate.
- **No API-key permission query.** The program could not prove that withdrawals are disabled.
- **Different fee assets.** Fees are charged in the received asset, or in MAX tokens when that deduction is on.

Paper trading on MAX prices has none of these problems.
