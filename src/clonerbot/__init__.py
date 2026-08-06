"""ClonerBot — autonomous crypto copy-trading bot.

Reads trade signals from Telegram channels, normalizes them, applies a strict
risk envelope, and executes spot trades across exchanges. Runs in paper mode by
default; live trading is gated behind an explicit config flag.
"""

__version__ = "0.1.0"
