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


async def _login() -> None:
    """One-time interactive Telegram login (creates the session file)."""
    from clonerbot.ingest.telegram_listener import TelegramListener

    settings = get_settings()
    if not (settings.tg_api_id and settings.tg_api_hash):
        print("TG_API_ID / TG_API_HASH are not set in .env — fill them first "
              "(get them at https://my.telegram.org).")
        return
    await TelegramListener(settings).login()


async def _check() -> None:
    """Validate config and exchange connectivity, then exit."""
    from clonerbot.db import init_db
    from clonerbot.exchange.router import ExchangeRouter

    settings = get_settings()
    print(f"mode={settings.mode.value} market={settings.market.value}")
    if settings.symbol_whitelist:
        print(f"whitelist={settings.symbol_whitelist} (only these traded)")
    else:
        print(f"blacklist={settings.symbol_blacklist} (all others traded)")
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


async def _backtest(args) -> None:
    """Replay logged signals against historical prices and rank channels."""
    from clonerbot.backtest.engine import Backtester
    from clonerbot.backtest.history import CcxtHistory
    from clonerbot.backtest.loader import load_signals
    from clonerbot.db import init_db

    settings = get_settings()
    await init_db()
    signals = await load_signals(channel=args.channel)
    if not signals:
        print("No replayable BUY signals found. Let the bot log some signals first.")
        return
    print(f"Loaded {len(signals)} signal(s). Fetching history from {args.exchange} "
          f"({args.timeframe}, up to {args.bars} bars each)…")
    src = CcxtHistory(args.exchange)
    bt = Backtester(src, timeframe=args.timeframe, bars=args.bars,
                    max_hold_bars=args.max_hold_bars, default_stop=settings.default_stop_loss)
    try:
        report = await bt.run(signals)
    finally:
        await src.close()

    print(f"\nTrades simulated: {report.total_trades} · skipped (no data): {report.skipped}\n")
    print(f"{'channel':<28} {'trades':>6} {'winrate':>8} {'avg_ret':>9} {'sum_ret':>9}")
    print("-" * 64)
    for cr in report.ranked():
        print(f"{cr.channel:<28} {cr.trades:>6} {cr.winrate:>7.0%} "
              f"{cr.avg_return:>8.2%} {cr.sum_return:>8.2%}")
    if report.total_trades:
        overall = sum(c.sum_return for c in report.per_channel.values())
        print("-" * 64)
        print(f"{'TOTAL sum of returns':<44} {overall:>8.2%}")


def _timeframe_minutes(tf: str) -> int:
    units = {"m": 1, "h": 60, "d": 1440}
    try:
        return int(tf[:-1]) * units.get(tf[-1], 1)
    except (ValueError, IndexError):
        return 5


async def _optimize(args) -> None:
    """Grid-search fixed risk parameters over the backtest and suggest env vars."""
    from clonerbot.backtest.history import CcxtHistory
    from clonerbot.backtest.loader import load_signals
    from clonerbot.backtest.optimize import Optimizer, default_grid, prefetch
    from clonerbot.db import init_db

    await init_db()
    signals = await load_signals(channel=args.channel)
    if not signals:
        print("No replayable BUY signals found. Let the bot log some signals first.")
        return
    print(f"Loaded {len(signals)} signal(s). Fetching history once from {args.exchange} "
          f"({args.timeframe})…")
    src = CcxtHistory(args.exchange)
    try:
        cached = await prefetch(src, signals, args.timeframe, args.bars)
    finally:
        await src.close()
    if not cached:
        print("No historical data could be fetched for these signals.")
        return

    grid = default_grid()
    print(f"Backtested {len(cached)} signals over {len(grid)} parameter combinations.\n")
    results = Optimizer(cached, min_trades=args.min_trades).run(grid)

    print(f"{'stop%':>6} {'tp%':>6} {'trail%':>7} {'hold':>5} "
          f"{'trades':>6} {'winrate':>8} {'avg_ret':>9} {'sum_ret':>9}")
    print("-" * 66)
    for r in results[:10]:
        c = r.combo
        print(f"{c.stop_pct:>6.0%} {c.tp_pct:>6.0%} {c.trailing_pct:>7.0%} "
              f"{c.max_hold_bars:>5} {r.trades:>6} {r.winrate:>7.0%} "
              f"{r.avg_return:>8.2%} {r.sum_return:>8.2%}")

    if results:
        best = results[0].combo
        tf_min = _timeframe_minutes(args.timeframe)
        print("\n✅ Best combination — set these in .env to apply live:")
        print(f"  CLONERBOT_STOP_LOSS_OVERRIDE_PCT={best.stop_pct}")
        print(f"  CLONERBOT_TAKE_PROFIT_OVERRIDE_PCT={best.tp_pct}"
              + ("   # 0 = keep signal's own TP" if best.tp_pct == 0 else ""))
        print(f"  CLONERBOT_TRAILING_STOP_PCT={best.trailing_pct}")
        print(f"  CLONERBOT_MAX_HOLD_MINUTES={best.max_hold_bars * tf_min}"
              + ("   # 0 = no time limit" if best.max_hold_bars == 0 else
                 f"   # {best.max_hold_bars} bars × {tf_min}m"))
        print("\n⚠️ Optimized on past data — validate in paper before going live.")


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
    parser.add_argument(
        "command", choices=["run", "login", "check", "stats", "backtest", "optimize"],
        nargs="?", default="run",
    )
    parser.add_argument("--json-logs", action="store_true", help="emit JSON logs")
    parser.add_argument("--log-level", default="INFO")
    # backtest options
    parser.add_argument("--exchange", default="binance",
                        help="exchange for historical OHLCV (backtest)")
    parser.add_argument("--timeframe", default="5m", help="candle timeframe (backtest)")
    parser.add_argument("--bars", type=int, default=288,
                        help="max candles fetched per signal (backtest)")
    parser.add_argument("--max-hold-bars", type=int, default=0,
                        help="close after N bars, 0=unlimited (backtest)")
    parser.add_argument("--channel", default=None, help="restrict to one channel (backtest/stats)")
    parser.add_argument("--min-trades", type=int, default=10,
                        help="min trades for a combo to qualify (optimize)")
    args = parser.parse_args()

    configure_logging(json_logs=args.json_logs, level=args.log_level)

    try:
        if args.command == "run":
            asyncio.run(_run())
        elif args.command == "login":
            asyncio.run(_login())
        elif args.command == "check":
            asyncio.run(_check())
        elif args.command == "stats":
            asyncio.run(_stats())
        elif args.command == "backtest":
            asyncio.run(_backtest(args))
        elif args.command == "optimize":
            asyncio.run(_optimize(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
