"""Two chains, one registry: derive an address on each and poll until paid.

Runs entirely against the in-memory mock chain, so it needs no network:

    python examples/two_chains.py
"""

from decimal import Decimal

from chainwatch_payments import PaymentRegistry, State
from chainwatch_payments.backends import MockBackend

btc = MockBackend("BTC", "testnet", decimals=8, default_confirmations=2)
usdt = MockBackend("USDT", "tron", decimals=6, default_confirmations=19)

registry = PaymentRegistry()
registry.register("BTC", "testnet", btc)
registry.register("USDT", "tron", usdt)

invoice_index = 42
btc_addr = registry.derive_address("BTC", "testnet", invoice_index)
usdt_addr = registry.derive_address("USDT", "tron", invoice_index)
print("pay BTC to ", btc_addr)
print("pay USDT to", usdt_addr)

# simulate the customers paying
btc.credit(btc_addr, "0.0005")
usdt.credit(usdt_addr, "25")

pending = {("BTC", "testnet", btc_addr): Decimal("0.0005"),
           ("USDT", "tron", usdt_addr): Decimal("25")}
tick = 0
while pending:
    tick += 1
    for (asset, network, addr), amount in list(pending.items()):
        s = registry.confirm(asset, network, addr, amount)
        print(f"t={tick:02d} {asset:<4} {s.state.value:<9} {s.match.value:<5} "
              f"{s.confirmations}/{s.required_confirmations} confs")
        if s.state is State.CONFIRMED:
            del pending[(asset, network, addr)]
    btc.mine(1)   # BTC blocks are slow...
    usdt.mine(5)  # ...TRON blocks are fast
print("all paid")
