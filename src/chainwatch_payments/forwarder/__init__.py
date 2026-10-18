"""OPT-IN hot-wallet forwarding (``pip install chainwatch-payments[forwarder]``).

Everything else in this package is watch-only. This subpackage is the one
place that derives private keys and signs transactions, so that payments that
land on one-time TRON addresses can be moved automatically to your own wallet.

Run it as a separate process (e.g. its own container) that is not reachable
from the internet, with the mnemonic of a DEDICATED wallet in an environment
variable. Never use the seed of the wallet that holds your savings.
"""
