# ClonerBot

Autonomous crypto **copy-trading bot**. It reads trade signals from Telegram
channels, normalizes them into a structured format, applies a strict risk
envelope, and executes **spot** trades across one or more exchanges. It runs in
**paper mode by default** — live trading is a single explicit flag you flip only
after you've reviewed paper-trading statistics.

> ⚠️ **Financial risk.** Autonomous trading on third-party Telegram signals can
> lose your entire deposit. Telegram signals are noisy and often low-quality or
> malicious. This software does not predict profit — it enforces discipline
> (position sizing, stop-losses, hard loss limits). Start in paper mode, keep
> the risk limits conservative, and never fund it with money you can't lose.

## Architecture

```
Telegram channels (Telethon user session)
      │  raw messages
      ▼
Signal Parser        regex rules + Anthropic LLM fallback → NormalizedSignal
      │              (unparseable → quarantine, never traded)
      ▼
Queue (in-proc / Redis, deduplicated)
      ▼
Risk & Decision Engine   whitelist · sizing · limits · daily loss · drawdown
      ▼
Exchange Router (CCXT)   picks exchange by balance/quote
      ▼
Executor  ── paper broker (virtual PnL)  │  live orders (behind CLONERBOT_MODE=live)
      │        with SL/TP monitoring + position reconciliation
      ▼
PostgreSQL (full audit)  +  Telegram control bot (status, PnL, KILL, manual withdrawal)
```

Every message and every decision is written to the `signals` table, so the
autonomous system is fully auditable.

## Quick start (paper mode, zero infra)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env      # then fill in the values (see below)
clonerbot run             # starts ingest → parse → risk → paper execution
```

With the defaults, storage is a local SQLite file and the queue is in-process,
so no Postgres/Redis is required for a first paper run.

### What you must fill into `.env`

| Purpose | Keys |
|---|---|
| Read signal channels | `TG_API_ID`, `TG_API_HASH` (from my.telegram.org), `TG_CHANNELS` |
| Control & withdrawals | `CONTROL_BOT_TOKEN` (BotFather), `CONTROL_ADMIN_IDS` (your user id) |
| Signal parsing | `ANTHROPIC_API_KEY` |
| Trading | `EXCHANGES` (JSON of spot API keys — **disable withdrawals on these keys**) |

All keys are prefixed `CLONERBOT_` — see `.env.example` for the full list and
the risk-limit tunables.

## Going live

1. Run in paper mode for at least a week. Review stats: `clonerbot stats`.
2. Confirm per-channel win-rates and that the parser isn't misreading messages.
3. Set `CLONERBOT_MODE=live`, fund a **small** amount, keep risk limits tight.
4. The KILL switch (`/kill` in the control bot) halts all trading instantly.

## Deployment

```bash
docker compose up -d     # bot + postgres + redis
```

The Telethon session is persisted in `./sessions` so you log in once.

## Commands

- `clonerbot run` — start the full pipeline.
- `clonerbot stats` — print channel reputation and PnL.
- `clonerbot check` — validate config and exchange connectivity, then exit.

## Safety notes

- Give exchange API keys **spot-trade permission only, withdrawals disabled**.
  Withdrawals are manual, initiated by you through the control bot.
- Keep `SYMBOL_WHITELIST` to liquid majors.
- `DAILY_LOSS_LIMIT` and `MAX_DRAWDOWN` auto-halt trading — they are the
  human's role in an otherwise autonomous loop.

## License

MIT
