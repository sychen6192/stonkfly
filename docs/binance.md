# Binance development setup

Status: **paper trading on Binance prices works; there is no Binance broker yet.** `stonkfly/binance.py` provides public observations for paper runs, a signed Spot REST client, key generation and a read-only account check. Nothing in this code submits a Binance order, and `run --live --exchange binance` is refused.

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

Testnet caveats:

- Funds are fake and the testnet is reset periodically; keys and balances can disappear.
- Testnet accounts hold many assets, so the live rule "start with only USDC" needs a testnet allocation instead.
- `/sapi` endpoints do not exist on testnet, so key-permission checks run only against the real exchange.
- Testnet prices and liquidity are not the real market. Testnet fills are plumbing evidence, not paper-trading performance.

## 2. Real-exchange key (read-only check)

1. Create an API key with **Enable Reading** and **Enable Spot & Margin Trading** only. Leave withdrawals, internal/universal transfer, margin and futures disabled, and restrict the key to your IP.
2. Run `python -m stonkfly binance-keygen binance-ed25519.pem` and register the public key, or use HMAC with `BINANCE_API_SECRET`.
3. Set `BINANCE_API_KEY` and the signer in `.env`.
4. Run `python -m stonkfly binance-check --live`. Still read-only. It reads `/sapi/v1/account/apiRestrictions` and fails if the key can withdraw or transfer funds (a missing field counts as enabled), and warns if the key is not IP-restricted.

Testnet and real keys use separate variables (`BINANCE_TESTNET_*` and `BINANCE_*`) so one can never be sent to the other network.

## Client guarantees

- No automatic retries. An HTTP 5XX sets `BinanceError.outcome_unknown`: Binance documents that status as unknown execution, so an order path must reconcile by client order ID, never resend.
- Request quantities must be `Decimal` or strings and are sent in plain notation; floats are rejected. Response numbers parse as `Decimal`.
- The API key header is sent only on signed requests. Signers redact their `repr`, and errors carry Binance's code and message without the request URL.

## Remaining work before Binance execution

- **Quote age:** Binance book responses carry no timestamp, so paper quotes use local receipt time. A live broker should also require a small `binance-check` clock offset.
- **Broker:** `LIMIT` orders with `timeInForce=FOK`, `newClientOrderId` from the ledger intent and `newOrderRespType=FULL`, with reconciliation through `origClientOrderId`. Fee ceilings come from `/api/v3/account/commission`, because Binance has no Coinbase-style preview.
- **Fee currency:** Binance charges buy fees in the base asset and sell fees in the quote asset, or in BNB when BNB fee payment is on. The ledger currently deducts every fee from cash. Turn BNB fee payment off and record fees per currency before any live order.
- **Account isolation:** Binance keys are account-wide. Use a dedicated sub-account where available, or restrict balance reconciliation to the managed assets.
- **Testnet run:** a broker for Spot Testnet, observing testnet prices rather than the real market, before any live order path.
