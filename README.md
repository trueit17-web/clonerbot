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
clonerbot login           # one-time Telegram login (phone + code)
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
4. The emergency stop (🛑 button in the control bot) halts all trading instantly.

## Deployment

### Keep it running after you log out (systemd)

Running `clonerbot run` in your SSH session stops when you disconnect. To keep
the bot running across logout, crashes and reboots, install it as a service:

```bash
cd ~/clonerbot
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
cp .env.example .env && nano .env       # fill in your keys
clonerbot login                          # one-time interactive Telegram login
bash deploy/install-service.sh           # install + start the systemd service
```

Then:

```bash
journalctl -u clonerbot -f               # live logs
sudo systemctl status clonerbot          # is it running?
sudo systemctl restart clonerbot         # after a git pull
sudo systemctl stop clonerbot            # stop
```

`clonerbot login` must be done once **before** starting the service (the
interactive login can't run under systemd); the saved `.session` file is reused
automatically afterwards.

### Docker (alternative)

```bash
docker compose up -d     # bot + postgres + redis
```

The Telethon session is persisted in `./sessions` so you log in once.

## Commands

- `clonerbot login` — one-time interactive Telegram login (creates the session
  file and verifies channel access). Run this once before `run`.
- `clonerbot run` — start the full pipeline.
- `clonerbot stats` — print channel reputation and PnL.
- `clonerbot check` — validate config and exchange connectivity, then exit.
- `clonerbot backtest` — replay logged signals against historical prices and
  rank channels by win rate / average return / total return. Options:
  `--exchange binance --timeframe 5m --bars 288 --max-hold-bars 0 --channel @x`.
  Uses public OHLCV (no keys); every signal is logged with its trade levels, so
  you can evaluate channels from history in minutes instead of weeks of paper.
- `clonerbot optimize` — hyperopt-lite: fetches history once, then grid-searches
  fixed risk parameters (stop %, take-profit %, trailing %, max-hold) over the
  logged signals and prints the best combination as ready-to-paste `.env` values
  (`STOP_LOSS_OVERRIDE_PCT`, `TAKE_PROFIT_OVERRIDE_PCT`, `TRAILING_STOP_PCT`,
  `MAX_HOLD_MINUTES`). When the overrides are set, the live risk engine uses
  those fixed levels instead of each signal's own. Validate in paper first.

## Autonomous learning (freqtrade-inspired)

Ideas ported from [freqtrade](https://github.com/freqtrade/freqtrade), adapted to
copy-trading:

- **Adaptive sizing (Edge-style expectancy).** The bot measures each channel's
  average return per closed trade and sizes accordingly — proven channels get
  more, and channels whose expectancy turns non-positive are **skipped
  automatically** (`USE_EXPECTANCY_SIZING`, `MIN_EXPECTANCY`,
  `EXPECTANCY_MIN_TRADES`). This is the core "learning" loop: allocation follows
  demonstrated results.
- **Protections (safety locks).** After a trade closes a channel enters a brief
  **cooldown**; a cluster of stop-losses trips a global **StoplossGuard** that
  pauses all trading; a **losing streak** locks the offending channel. Locks are
  time-bounded, persisted, and enforced by the risk engine.
- **Time-based exit.** Optionally close any position still open after
  `MAX_HOLD_MINUTES`.

Deliberately **not** ported (different problem / far larger scope): freqtrade's
indicator-strategy engine, backtesting, hyperopt and FreqAI. Our "backtest" is
the paper-trading + expectancy loop; historical-signal backtesting is a possible
future addition since every signal is already logged.

## Channel discovery (optional, OFF by default)

The bot can find candidate signal channels for you instead of you listing them
all by hand. Set `CLONERBOT_DISCOVERY_ENABLED=true` and it will periodically
search public channels by `DISCOVERY_KEYWORDS` and surface candidates. It never
auto-joins and never auto-trusts — the flow is deliberately gated:

```
discover → you /approve → JOIN + OBSERVE (paper-only) → auto-promote → ACTIVE (real)
```

- **Discovery proposes, you approve.** Auto-joining channels risks a Telegram
  ban on your account, so joins happen only when you tap ✅ Одобрить and are
  rate-limited by `JOIN_COOLDOWN_SEC`.
- **New channels trade paper first.** An approved channel is `OBSERVING`: its
  signals run through a shadow (paper) executor and never touch real money —
  even in live mode — until it earns trust.
- **Promotion is by demonstrated results.** After `PROMOTE_MIN_TRADES` closed
  paper trades with win rate ≥ `PROMOTE_MIN_WINRATE` (and positive PnL), the
  channel is promoted to `ACTIVE` and becomes eligible for real orders. If a
  live channel's win rate later falls below `DEMOTE_WINRATE`, it's auto-demoted
  back to paper.

The bot pushes a message to admins on every trade **open** and **close** (with
PnL) and on channel **promotion/demotion** — so you can watch paper activity
live and see which channels actually perform (toggle with `NOTIFY_TRADES`).
The 🏆 Рейтинг каналов and 🧾 История сделок views summarize the same data.

The control bot is fully button-driven (Russian UI): open it with `/start` to
get the menu — 📊 Статус · 📈 Позиции · 🧾 История сделок · 🏆 Рейтинг каналов ·
➕ Добавить канал · 📋 Кандидаты · 🔎 Искать каналы · 🛑 Стоп-торговля ·
▶️ Возобновить · 💸 Вывод · ⚙️ Настройки.

**⚙️ Настройки** shows per-exchange connection status (🔌 Статус бирж — a live
check of reachability, key validity and balances **across account types**, so
funds in a Unified/Funding wallet aren't misreported as 0), and lets you add
(➕), remove (🗑) exchanges and toggle 🔴 LIVE / 🧪 paper right from the bot
(going LIVE asks for confirmation; the choice persists across restarts). Keys
added via the bot are stored in the DB and merged with `.env` on startup. Startup also logs an
`exchange.status` line per exchange, so "did my keys connect?" is answerable
straight from `journalctl`. ⚠️ Sending API secrets through Telegram puts them in
chat history — give keys **spot-only, withdrawals disabled**, and delete the
message afterwards. Candidates are approved or rejected with inline
✅ / 🚫 buttons; the emergency stop asks for confirmation before closing
everything. **➕ Добавить канал** adds a channel by @name (it starts in paper
observe, same safe path as discovered ones). The channel-trust machinery is
always on; only the periodic auto-search (🔎, incl. the optional tgstat source
via `DISCOVERY_USE_TGSTAT`) is gated by `DISCOVERY_ENABLED`.

## Safety notes

- Give exchange API keys **spot-trade permission only, withdrawals disabled**.
  Withdrawals are manual, initiated by you through the control bot.
- Keep `SYMBOL_WHITELIST` to liquid majors.
- `DAILY_LOSS_LIMIT` and `MAX_DRAWDOWN` auto-halt trading — they are the
  human's role in an otherwise autonomous loop.

## License

MIT
