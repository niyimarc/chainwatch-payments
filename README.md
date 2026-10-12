# chainwatch-payments

Watch-only, multi-chain crypto payment detection for Python. No web framework,
no payment gateway, no private keys.

It does two things, for whichever coins and networks you enable:

1. **Derive a receive address** for an `(asset, network)` pair from an extended
   public key (xpub).
2. **Tell you whether a payment to that address has arrived and confirmed**,
   with confirmation counts, reorg handling and an under/over-payment
   tolerance band.

Everything else (plans, invoices, pricing, renewals, what to do on
non-payment) belongs to your application.

```
pip install chainwatch-payments                # core only, zero dependencies
pip install "chainwatch-payments[bitcoin]"     # + BTC default source
pip install "chainwatch-payments[tron]"        # + TRON TRC-20 default source
pip install "chainwatch-payments[evm]"         # + Ethereum/EVM ERC-20 default source
pip install "chainwatch-payments[all]"
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
on Sepolia. Any other token: pass `contract=` and `decimals=`. **Check contract
addresses against the issuer's official list before going live.**

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

## Testing

```
pip install -e ".[all,test]"
pytest
```

The suite never touches a real chain: chain access is mocked at the HTTP layer
(and a guard fails any test that tries to build a real client). It covers
registry routing, derivation vectors (BIP32 test vector 1, BIP44/49/84, ETH,
TRON; on both the accelerated and pure-Python paths), NONE/SEEN/CONFIRMED
transitions, tolerance bands, reorg reversal, idempotency, `not_before`, both
single-address discriminators, and several backends enabled at once. Use
testnets (BTC testnet/testnet4/signet, TRON Nile, Sepolia) for manual checks.

## Versioning

Semantic versioning. `0.x` while the API settles: minor versions may change the
public interface, patch versions will not.

## Licence

MIT
