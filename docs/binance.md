# Binance development setup

Status: **paper trading on Binance prices and order execution on Spot Testnet work; real-exchange orders are not implemented.** `stonkfly/binance.py` provides public observations, a Spot Testnet broker, a signed REST client, key generation and a read-only account check. `run --live --exchange binance` is refused, and the broker refuses any client not pointed at Spot Testnet.

## Paper trading on Binance prices

```sh
python -m stonkfly run --exchange binance
python -m stonkfly status --out runs/binance-paper
```

No key is needed. Observations come from the public market-data endpoint: completed one-minute klines seed the chart, and every observation re-reads `exchangeInfo` (status, spot permission, `LIMIT` support, tick size, step size, minimum quantity and notional) plus the best bid/ask. Anything missing or unexpected stops the run instead of guessing. Paper fills charge 0.1%, Binance's regular-tier spot fee; your account's rate can differ (`binance-check` shows it).

The chart, neural model, decoder and limits are unchanged; only the price source and fee differ. Runs default to `runs/binance-paper`, and the ledger refuses to mix exchanges in one run directory.

## Endpoints

| Network | Base URL | Use |
| --- | --- | --- |
| Spot Testnet (default) | `https://testnet.binance.vision` | Development with test funds |
| Real exchange | `https://api.binance.com` | Your account; must be named explicitly (`--live`) |
| Public market data | `https://data-api.binance.vision` | Prices only, no key; intended for paper observations |

Binance refuses some regions with HTTP 451. Run from where your account is permitted.

## 1. Spot Testnet key

1. Sign in at [testnet.binance.vision](https://testnet.binance.vision) with GitHub.
2. Ed25519 (recommended): run `python -m stonkfly binance-keygen binance-testnet-ed25519.pem`. It writes the private key with mode 600, refuses to overwrite an existing file, and prints the public key. Register that public key on the testnet page and copy the API key it shows. HMAC also works: generate an HMAC key and set `BINANCE_TESTNET_API_SECRET` instead of the key file.
3. Copy `.env.example` to `.env` and set `BINANCE_TESTNET_API_KEY`. Default `*.pem` names are git-ignored; keep custom paths outside the repository.
4. Run `python -m stonkfly binance-check`. It uses GET requests only and reports clock offset, symbol filters (tick size, step size, minimum notional), best bid/ask, trade permission, quote-currency balance, commission rates and open orders. It exits 1 and lists `problems` if anything blocks development.

## 2. Spot Testnet trading

```sh
python -m stonkfly run --exchange binance --testnet --preflight-only
python -m stonkfly run --exchange binance --testnet
python -m stonkfly status --out runs/binance-testnet
```

This sends real orders to Spot Testnet, with test funds only. Observations come from the testnet book, because that is where the orders fill. The run directory defaults to `runs/binance-testnet`.

Testnet accounts hold many assets, so the broker trades a **100 USDC allocation**. At initialization it records the free balances of the managed assets (USDC plus each product's base asset; BNB and others are ignored) and requires a fresh ledger, at least 100 USDC, no locked managed balance and no open orders. Afterwards every managed balance must equal that baseline plus the ledger's own fills, and a managed symbol may have no open order. Any difference halts the run.

Each order is a `LIMIT` `FOK` order with the ledger intent as `newClientOrderId`:

1. `POST /api/v3/order/test` with `computeCommissionRates` validates the order without placing it. A test-order error, missing fee rates, a fee above the ceiling, BNB fee payment switched on, a stale quote, a STOP file or a balance mismatch rejects the intent before anything can be placed.
2. The intent is marked `UNKNOWN` in SQLite, then `POST /api/v3/order` is sent once.
3. The final order and its trades (`GET /api/v3/order`, `GET /api/v3/myTrades`) settle the ledger. Buy fees come out of the base asset and sell fees out of USDC, as Binance charges them. A fee in any other asset stops the run before booking.

A documented 4XX rejection books nothing. A 5XX, error codes -1006/-1007, a timeout or a lost reply leave the intent `UNKNOWN` and stop the run. On restart the broker looks the order up by client order ID and settles it; if Binance has no such order, the run stays stopped for manual review and never resends. After checking, restart with `--resume-reviewed`.

Testnet caveats:

- Funds are fake and the testnet is reset periodically; keys and balances can disappear.
- A testnet reset changes the balances, which halts the run as an external change. Start a new run directory afterwards.
- If `binance-check` reports that a USDC pair is unavailable on testnet, testnet trading cannot run for that product.
- `/sapi` endpoints do not exist on testnet, so key-permission checks run only against the real exchange.
- Testnet prices and liquidity are not the real market. Testnet fills are plumbing evidence, not paper-trading performance.

## 3. Real-exchange key (read-only check)

1. Create an API key with **Enable Reading** and **Enable Spot & Margin Trading** only. Leave withdrawals, internal/universal transfer, margin and futures disabled, and restrict the key to your IP.
2. Run `python -m stonkfly binance-keygen binance-ed25519.pem` and register the public key, or use HMAC with `BINANCE_API_SECRET`.
3. Set `BINANCE_API_KEY` and the signer in `.env`.
4. Run `python -m stonkfly binance-check --live`. Still read-only. It reads `/sapi/v1/account/apiRestrictions` and fails if the key can withdraw or transfer funds (a missing field counts as enabled), and warns if the key is not IP-restricted.

Testnet and real keys use separate variables (`BINANCE_TESTNET_*` and `BINANCE_*`) so one can never be sent to the other network.

## Client guarantees

- No automatic retries. An HTTP 5XX sets `BinanceError.outcome_unknown`: Binance documents that status as unknown execution, so an order path must reconcile by client order ID, never resend.
- Request quantities must be `Decimal` or strings and are sent in plain notation; floats are rejected. Response numbers parse as `Decimal`.
- The API key header is sent only on signed requests. Signers redact their `repr`, and errors carry Binance's code and message without the request URL.

Book responses carry no timestamp, so quotes use local receipt time. The testnet broker also refuses to start with a clock offset of 1 s or more.

## Remaining work before real-exchange orders

- **Key permissions in preflight:** refuse keys that can withdraw, transfer, borrow on margin or trade futures, as `binance-check --live` already reports.
- **Account isolation:** decide between a dedicated sub-account (where Binance allows one) and the testnet-style allocation baseline in a shared account, where any manual trade in a managed asset halts the run.
- **Explicit opt-in:** the same `STONKFLY_LIVE` and `--live` double opt-in as Coinbase, a separate run directory, and a testnet run reviewed first.
