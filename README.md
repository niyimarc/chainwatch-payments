# chainwatch-payments

Watch-only, multi-chain crypto payment detection for Python. No payment
gateway, no private keys. The core is framework-neutral.

Optional extras add:
* **`[django]`:** invoice models, a poll command and signals;
* **`[forwarder]`:** opt-in forwarders that move received funds to your wallet
  (TRON, BTC, ERC-20). This is the only part that holds keys, and it runs in
  its own process.

The Django setup is documented in [DJANGO_DOC.md](DJANGO_DOC.md).

It does two things, for whichever coins and networks you enable:

1. **Derive a receive address** for an `(asset, network)` pair from an extended
   public key (xpub).
2. **Tell you whether a payment to that address has arrived and confirmed**,
   with confirmation counts, reorg handling and an under/over-payment
   tolerance band.

It also includes optional pricing helpers (CoinGecko and fixed stablecoin
rates, plus a quote calculator) and the Django integration. Plans,
subscriptions, renewals and what to do on non-payment stay in your
application.

```
pip install chainwatch-payments                # core only, zero dependencies
pip install "chainwatch-payments[bitcoin]"     # + BTC default source
pip install "chainwatch-payments[tron]"        # + TRON TRC-20 default source
pip install "chainwatch-payments[evm]"         # + Ethereum/EVM ERC-20 default source
pip install "chainwatch-payments[all]"
pip install "chainwatch-payments[django]"     # + Django integration
pip install "chainwatch-payments[forwarder]"  # + opt-in forwarders, TRON/BTC/ERC-20 (hold keys; see DJANGO_DOC.md)
pip install "chainwatch-payments[fast]"       # optional libsecp256k1 speed-up
```

The core is pure Python. The chain extras add `httpx` (the default HTTP
transport for the shipped chain sources) and, for TRON/EVM, `pycryptodome` for
a fast Keccak. `[fast]` adds `coincurve` (libsecp256k1) for quicker address
derivation; it is optional and may need a C compiler on very new Python
versions. Without the accelerators the package uses its own pure-Python
implementations, which produce identical results and are tested against the
same vectors.

## The watch-only model

* Each backend is configured with an **account-level extended public key**
  (e.g. `m/84'/0'/0'` for BTC native SegWit, `m/44'/195'/0'` for TRON,
  `m/44'/60'/0'` for Ethereum) or a single public address.
* Addresses are derived with **non-hardened public derivation only**:
  `<account xpub>/0/<index>`. The package has no code path that accepts,
  derives or uses a private key. Extended private keys (`xprv`, `zprv`,
  `tprv`, ...) are rejected with `PrivateKeyRejected`.
* It **never sends, sweeps or refunds**. Chain access is read-only (explorer
  REST APIs or JSON-RPC).
* It is **stateless**. Each `confirm()` re-reads the chain and computes the
  status from scratch, so results are deterministic and idempotent, and a
  reorg that drops a transaction is reflected on the next call.

Your spending keys stay in your wallet. Export the account xpub from it (most
wallets call this "account extended public key") and give only that to your
server.

## Shipped backends

| Asset | Backend | Networks | Default source | Default confirmations |
|---|---|---|---|---|
| BTC | `BitcoinBackend` | mainnet, testnet, testnet4, signet, regtest | Esplora REST (blockstream.info / mempool.space) | 2 |
| USDT (any TRC-20) | `TronTRC20Backend` | mainnet, nile, shasta | TronGrid | 19 (TRON solidification) |
| USDT, USDC (any ERC-20) | `ERC20Backend` | ethereum, sepolia, any EVM chain via `rpc_url` | Standard JSON-RPC | 12 |
| anything | `MockBackend` | simulated | in-memory chain | configurable |

Token presets: USDT on TRON mainnet and Nile; USDT and USDC on Ethereum; USDC
on Sepolia, all verified against the issuers (see "Verified presets and live
check"). Any other token: pass `contract=` and `decimals=`, and verify that
contract yourself.

**Using your own node:** every shipped backend takes a `source=` (or
`base_url=` / `rpc_url=`). Point `EsploraSource` at your own `electrs` or
`mempool` instance, point `ERC20Backend(rpc_url=...)` at your own geth/erigon/
reth node, or implement the one-method `ChainSource` protocol for anything else.

## Usage: two chains, derive, poll

```python
import time
from datetime import datetime, timezone
from decimal import Decimal

from chainwatch_payments import PaymentRegistry, State, Tolerance
from chainwatch_payments.backends import BitcoinBackend, TronTRC20Backend

registry = PaymentRegistry(default_tolerance=Tolerance.absolute("0.01"))

registry.register("BTC", "testnet", BitcoinBackend(
    "vpub5Y6cjg78GGuNLsaPhmYsiw4gYX3HoQiRBiSwDaBXKUafCt9bNwWQiitDk5VZ5BVxYnQdwoTyXSs2JHRPAgjAvtbBrf8ZhDYe2jWAqvZVnsc",
    network="testnet",
    confirmations=1,
))
registry.register("USDT", "tron-nile", TronTRC20Backend(
    "xpub6D1AabNHCupeiLM65ZR9UStMhJ1vCpyV4XbZdyhMZBiJXALQtmn9p42VTQckoHVn8WNqS7dqnJokZHAHcHGoaQgmv8D45oNUKx6DZMNZBCd",
    network="nile",
    api_key="your-trongrid-key",   # optional, raises rate limits
))

# Your app owns the index: e.g. the invoice's primary key. Never reuse one.
invoice_id = 1001
created_at = datetime.now(timezone.utc)

btc_addr = registry.derive_address("BTC", "testnet", invoice_id)
usdt_addr = registry.derive_address("USDT", "tron-nile", invoice_id)

pending = {
    ("BTC", "testnet", btc_addr): Decimal("0.0005"),
    ("USDT", "tron-nile", usdt_addr): Decimal("25"),
}
last = {}

while pending:
    for (asset, network, addr), amount in list(pending.items()):
        status = registry.confirm(
            asset, network, addr, amount,
            not_before=created_at,
            tolerance=Tolerance.exact() if asset == "BTC" else None,  # None = registry default
            previous=last.get(addr),
        )
        last[addr] = status
        if status.reverted:
            print(f"{asset}: previously confirmed payment was reorged out!")
        print(asset, status.state.value, status.match.value,
              f"{status.received}/{status.expected}", f"{status.confirmations} confs")
        if status.state is State.CONFIRMED:
            del pending[(asset, network, addr)]   # your app marks the invoice paid
    time.sleep(30)
```

The registry key is your own label: `("USDT", "tron-nile")` above is just a
name for that configured backend. Register as many as you like at once, each
with its own key, source and confirmation depth.

A fully runnable version against the mock chain is in
[`examples/two_chains.py`](examples/two_chains.py).

(The xpubs above belong to the public BIP39 test mnemonic
"abandon abandon ... about". Never send real funds to them.)

## PaymentStatus

```python
PaymentStatus(
    state=State.NONE | State.SEEN | State.CONFIRMED,
    match=Match.NONE | Match.EXACT | Match.UNDER | Match.OVER,
    expected_amount, received_amount, confirmed_amount,   # integer base units
    expected, received, confirmed,                        # Decimal human units (properties)
    confirmations, required_confirmations, tx_ids,
    reverted,        # True if `previous` was CONFIRMED and this one is not
    is_paid,         # == (state is CONFIRMED)
)
```

* **NONE**: nothing to this address (after `not_before`).
* **SEEN**: something arrived, but the value with enough confirmations does not
  yet cover the expected amount. `match` tells you whether what is visible so
  far is UNDER, EXACT or OVER.
* **CONFIRMED**: confirmed value is at least `expected - under tolerance`.
  `match` is EXACT within the band, OVER above it. An underpayment never
  reaches CONFIRMED.
* **Reorgs**: a CONFIRMED status can go back to SEEN or NONE if its
  transaction drops or loses depth. Pass the last status as `previous=` and
  check `reverted`. Choose `required_confirmations` so this is rare for the
  amounts involved.
* **Errors**: if the explorer/node can't be read, `confirm()` raises
  `ChainSourceError`. A failed read is never reported as "no payment".

Amounts are always `Decimal`, `str` or `int` in the asset's human units.
Floats are refused.

## Address modes

* **HD one-time addresses (default, recommended).** One derived address per
  payment; all credits to it are summed. Unambiguous on every chain.
* **Single address + discriminator.** Configure `address=` instead of (or as
  well as) an xpub and use `registry.receive_address(asset, network)`. Then
  either:
  * memo/tag: `registry.address_with_memo(...)` and `confirm(..., memo=...)`
    on backends whose `capabilities().memo` is true (none of the shipped
    chains have memos; this is for custom backends such as XRP/XLM), or
  * unique amount: `unique_amount("10", slot, "0.000001", 6)` and
    `confirm(..., match_mode=MatchMode.SINGLE)`. Only a single credit within
    the band counts. Different amounts may be someone else's payment, so
    under/over can't be detected in this mode.

## Adding a chain: the Backend protocol

A backend is any object with these three methods. No subclassing needed.

```python
from datetime import datetime
from chainwatch_payments import Capabilities, Transfer

class MyChainBackend:
    def capabilities(self) -> Capabilities:
        return Capabilities(
            hd_derivation=True,        # derive_address works
            memo=False,                # address_with_memo works
            decimals=8,                # base units per coin = 10**decimals
            default_confirmations=6,
        )

    def derive_address(self, index: int) -> str:
        ...  # public derivation only

    def get_incoming(self, address: str, not_before: datetime | None = None) -> list[Transfer]:
        # Every credit to `address`, in integer base units:
        # Transfer(tx_id, amount, confirmations, timestamp=None, index=0,
        #          memo=None, block_height=None)
        # `index` separates several credits inside one tx (output/log index).
        # Raise ChainSourceError if the chain can't be read.
        ...

    # optional
    def receive_address(self) -> str: ...
    def address_with_memo(self, reference: str) -> tuple[str, str]: ...

registry.register("DOGE", "mainnet", MyChainBackend())
```

For xpub-based chains, subclass `chainwatch_payments.backends.HDBackend` and
pass an address `encoder`; you get account-depth checks, address validation
and single-address mode for free. The confirmation logic (`evaluate`) is
shared by every backend, so a new chain only has to report transfers.

## Notes per chain

* **ERC-20 one-time addresses** hold tokens but no ETH, so moving the funds
  later needs gas sent to each address first. That sweeping step is outside
  this package; factor it into whether HD or single-address suits you.
* **ERC-20 scanning** reads logs over a block window: either estimated from
  `not_before` (recommended, cheap) or the last `lookback_blocks` (default
  20,000). Tune `chunk_size` to your RPC provider's `eth_getLogs` limit.
* **TRON** needs one `gettransactioninfobyid` call per matching transfer to get
  its block and execution result; failed (reverted) transfers are ignored.
* **BTC** credits are counted per output; change outputs to other addresses in
  the same transaction are ignored.

## Pricing (framework-neutral)

Turn a fiat price into the exact crypto amount to request:

```python
from chainwatch_payments import (ChainedPriceProvider, CoinGeckoProvider, CrossCheckedPriceProvider,
                                 FixedRateProvider, KrakenProvider, quote)

prices = ChainedPriceProvider([
    FixedRateProvider(),                              # USDT/USDC = 1 USD, no API call
    CrossCheckedPriceProvider(
        CoinGeckoProvider("your-demo-key"),           # primary, cached 60 s
        KrakenProvider(),                             # reference
        max_deviation="0.02",                         # refuse to quote if they differ by > 2%
    ),
])
rate = prices.rate("BTC", "USD")                      # Decimal, fiat per 1 BTC

q = quote(asset="BTC", fiat="USD", fiat_price="20", rate=rate, decimals=8,
          volatility_buffer="0.01",                   # +1% for price movement during the payment window
          network_fee_fiat="0.90")                    # passed on to the customer
q.total_amount   # (20 * 1.01 + 0.90) / rate, rounded UP to 8 decimals
```

If a rate can't be fetched, or the two sources disagree, `PriceUnavailable` is
raised: never quote without a trustworthy price. CoinGecko plans: no key
(`plan="public"`, development only), a free Demo key (`plan="demo"`, the
default when a key is given) or a paid key (`plan="pro"`). Kraken needs no
key. Any object with `rate(asset, fiat) -> Decimal` can be a provider.

### Dynamic network fees

The fee you pass on can follow the network instead of being a fixed number:

```python
from chainwatch_payments.fees import MempoolSpaceFeeEstimator, TronEnergyFeeEstimator, estimate_fee_fiat

btc_fee = estimate_fee_fiat(MempoolSpaceFeeEstimator(), fiat="USD", prices=prices,
                            minimum=Decimal("0.50"), margin=Decimal("0.2"))
```

`estimate_fee_fiat` returns `max(minimum, estimate * (1 + margin))`. If the
estimator fails it falls back to the minimum.

* **BTC (`MempoolSpaceFeeEstimator`):** mempool.space's recommended sat/vB
  (default target `halfHourFee`) times `vbytes` (default 110, a
  1-input/1-output transaction).
* **TRON (`TronEnergyFeeEstimator`):** the chain's current energy and
  bandwidth prices times the energy and bandwidth one USDT transfer uses
  (defaults 130,000 energy and 350 bytes). The energy default comes from a
  real mainnet USDT transfer in October 2026: TRON adds a dynamic-energy
  penalty on busy contracts, so the cost is about twice the 65,000 base.

## Async (FastAPI and friends)

```python
from chainwatch_payments.aio import AsyncPaymentRegistry

areg = AsyncPaymentRegistry(registry)
status = await areg.confirm("BTC", "mainnet", address, "0.001")
results = await areg.confirm_many([dict(asset="BTC", network="mainnet", address=a, expected_amount="0.001")
                                   for a in addresses], concurrency=8)
```

Chain reads use blocking HTTP, so each call runs in a worker thread
(`asyncio.to_thread`) and your event loop stays free. In `confirm_many`, a
failed read comes back as the exception in its position; the other results
are kept.

## Django integration

The optional `[django]` extra turns the package into a reusable Django app:
an abstract invoice model to inherit, `create_invoice()` at checkout, a
`chainwatch_poll` command, signals such as `invoice_paid`, late-payment
pricing at the price when the payment arrived, an audit trail, an admin
mixin and opt-in forwarders that move funds to your wallet (TRON, BTC,
ERC-20).

```
pip install "chainwatch-payments[django,bitcoin,tron]"
```

**Full setup guide: [DJANGO_DOC.md](DJANGO_DOC.md)**, covering installation,
the settings reference, the model, checkout, polling, signals, statuses, late
payments, single-address mode, the admin, forwarding and security notes.

## Verified presets and live check

The token contract presets were checked against the issuers in October 2026:

| Preset | Contract | Source |
|---|---|---|
| USDT on TRON | `TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t` | Tether's supported-protocols page |
| USDT on Ethereum | `0xdAC17F958D2ee523a2206206994597C13D831ec7` | Tether's supported-protocols page |
| USDC on Ethereum | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | Circle's USDC contract addresses page |
| USDC on Sepolia | `0x1c7D4B196Cb0C7B01d743Fbc6116a902379C7238` | Circle's USDC contract addresses page |
| USDT on TRON Nile | `TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf` | On chain: token "Tether USD", 6 decimals (the standard Nile test token) |

The API formats the package reads (Esplora, TronGrid, TRON chain parameters,
mempool.space, Kraken and CoinGecko) were also checked against the live
services and their docs.

Before going live, run the read-only check yourself. It needs no keys or funds:

```
pip install -e ".[all]"
python scripts/live_check.py        # add -v for tracebacks
```

It reads real data on BTC mainnet and testnet4, TRON mainnet and Nile,
Ethereum mainnet and Sepolia, mempool.space, CoinGecko and Kraken, and prints
PASS or FAIL per check. Then do one real payment per chain on a testnet
(testnet4, Nile, Sepolia) with your own wallet, and confirm the address the
package shows matches your wallet's address for that index.

## Testing

```
pip install -e ".[all,django,forwarder,test]"
pytest
```

The suite never touches a real chain or price feed: everything is mocked at
the HTTP layer, and a guard fails any test that tries to build a real
client.

* **Core:** routing, derivation vectors (BIP32/44/49/84, ETH, TRON, on both the
  accelerated and pure-Python paths), confirmation states, tolerance, reorgs,
  idempotency, discriminators, retries, pricing, cross-checks, fee estimators,
  the async facade and the forwarders. Bitcoin signatures are checked against
  a separate BIP143 implementation with a different ECDSA library, and
  Ethereum transactions are decoded and their signer recovered.
* **Django** (needs Django and pytest-django; skipped otherwise):
  * creation, index allocation and every status transition;
  * payment-time pricing for late payments, both late rules and their windows;
  * backoff, the audit trail, the gap warning and single-address mode;
  * dynamic fees, signals firing once, failure isolation;
  * commands, admin and the forwarders (once-only, sent to confirmed,
    dropped retries, waiting on fees without using up attempts, config checks).

By default the Django tests use SQLite. To run them on PostgreSQL, including
the concurrency tests that hammer index allocation from parallel threads:

```
CHAINWATCH_TEST_POSTGRES=localhost:5432 pytest tests/django_tests   # user postgres, trust auth
```

CI (`.github/workflows/ci.yml`) runs:
* ruff and mypy;
* the core with no extras on Python 3.10 to 3.14;
* everything with all extras on Python 3.10 to 3.14;
* Django 4.2;
* PostgreSQL;
* a build plus `twine check`.

## Versioning

Semantic versioning; see `CHANGELOG.md`. While the version is `0.0.x` the API
may still change between releases. The version is defined once, in
`src/chainwatch_payments/__init__.py` (`__version__`).

## Licence

MIT; see `LICENSE`.
