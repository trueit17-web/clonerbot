"""CLI entrypoint: `clonerbot run | check | stats`."""

from __future__ import annotations

import argparse
import asyncio
import sys

from clonerbot.config import get_settings
from clonerbot.logging_conf import configure_logging, get_logger

log = get_logger("main")


async def _run() -> None:
    from clonerbot.core.app import Application

    settings = get_settings()
    app = Application(settings)
    await app.run()


async def _check() -> None:
    """Validate config and exchange connectivity, then exit."""
    from clonerbot.db import init_db
    from clonerbot.exchange.router import ExchangeRouter

    settings = get_settings()
    print(f"mode={settings.mode.value} market={settings.market.value}")
    print(f"whitelist={settings.symbol_whitelist}")
    print(f"risk_per_trade={settings.risk_per_trade} max_open={settings.max_open_positions} "
          f"daily_loss={settings.daily_loss_limit} max_dd={settings.max_drawdown}")
    print(f"anthropic={'set' if settings.anthropic_api_key else 'MISSING'}")
    print(f"telegram_ingest={'set' if settings.tg_api_id else 'MISSING'} "
          f"channels={settings.tg_channels}")
    print(f"control_bot={'set' if settings.control_bot_token else 'MISSING'} "
          f"admins={settings.control_admin_ids}")
    await init_db()
    print("db: OK")
    router = ExchangeRouter(settings)
    if router.has_exchanges:
        await router.load()
        for ex_id, client in router.clients.items():
            try:
                price = await client.fetch_price("BTC/USDT")
                print(f"exchange {ex_id}: OK (BTC/USDT={price})")
            except Exception as exc:
                print(f"exchange {ex_id}: ERROR {exc}")
        await router.close()
    else:
        print("exchanges: none configured (paper price discovery will be limited)")
    print("check complete.")


async def _stats() -> None:
    from clonerbot.db import init_db
    from clonerbot.scoring.channel_scorer import ChannelScorer

    await init_db()
    rows = await ChannelScorer().all_stats()
    if not rows:
        print("No channel stats yet.")
        return
    print(f"{'channel':<28} {'signals':>7} {'closed':>7} {'winrate':>8} {'pnl':>12}")
    for r in sorted(rows, key=lambda x: x.cumulative_pnl, reverse=True):
        wr = (r.wins / r.trades_closed) if r.trades_closed else 0.0
        print(f"{r.channel:<28} {r.signals_total:>7} {r.trades_closed:>7} "
              f"{wr:>7.0%} {r.cumulative_pnl:>12,.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="clonerbot", description="Autonomous copy-trading bot")
    parser.add_argument("command", choices=["run", "check", "stats"], nargs="?", default="run")
    parser.add_argument("--json-logs", action="store_true", help="emit JSON logs")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    configure_logging(json_logs=args.json_logs, level=args.log_level)

    try:
        if args.command == "run":
            asyncio.run(_run())
        elif args.command == "check":
            asyncio.run(_check())
        elif args.command == "stats":
            asyncio.run(_stats())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
