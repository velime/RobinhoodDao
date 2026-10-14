"""Own signal scanners: OI, funding, spreads, deposits/withdrawals, DEX dumps.

Each scanner runs on its own (`python -m bottom.signals oi`, ... or `all`) and posts into its topic of a
Telegram forum group. Everything it posts is also stored in the signals database, which Bottom reads.
"""
