# Binance development setup

Status: **development scaffolding only.** `stonkfly/binance.py` provides a signed Spot REST client, key generation and a read-only account check. There is no Binance market adapter or broker yet: `run` still observes Coinbase and trades only through the Coinbase broker. Nothing in this code submits a Binance order.

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

- **Market adapter:** klines, `exchangeInfo` filters and best bid/ask from the public data endpoint. Spot depth responses carry no timestamp, so quote age must come from local receipt time.
- **Broker:** `LIMIT` orders with `timeInForce=FOK`, `newClientOrderId` from the ledger intent and `newOrderRespType=FULL`, with reconciliation through `origClientOrderId`. Fee ceilings come from `/api/v3/account/commission`, because Binance has no Coinbase-style preview.
- **Fee currency:** Binance charges buy fees in the base asset and sell fees in the quote asset, or in BNB when BNB fee payment is on. The ledger currently deducts every fee from cash. Turn BNB fee payment off and record fees per currency before any live order.
- **Account isolation:** Binance keys are account-wide. Use a dedicated sub-account where available, or restrict balance reconciliation to the managed assets.
- **Wiring:** an explicit exchange selection in the CLI, provenance feed labels, and a separate run directory for Binance.
