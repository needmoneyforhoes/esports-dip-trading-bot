# esports-dip-trading-bot

Self-contained live dip-buy / momentum-reversion bot for Polymarket esports series-moneyline
(Dota 2) markets. **Paper-trades by default.**

## Why it exists

In a typical esports series-moneyline market, a team that craters to ~25¢ is usually genuinely
losing and gets closed out — so naive dip-buying dies (−100%). This package is a disciplined,
two-trigger dip-buy/reversion rule plus an honest measurement harness: it paper-trades against
the **real** order book so you can verify whether a *specific subset* of close/marquee matches
is tradable before risking a cent. Backtest on the full 142-match Dota population is
**negative-EV even at zero cost** (≈ −0.11/$1); the earlier +0.18/$1 was top-50-by-volume
selection bias. It ships PAPER-first on purpose.

## How it works

The strategy is two triggers, run as an independent state machine per team side
(`IDLE → WATCH → HOLD → TRAIL`):

- **Entry** — once a side dips to `--entry` (0.25), wait; never buy while it's still making new
  lows. Buy when it either holds `--x` seconds without a new low **or** bounces `--gain-up` off
  the low. Floored by `--min-entry` (skip corpses), never chases above `--max-entry`.
- **Exit** — hold with no early stop until price reaches `--target` (0.50), then trail the high:
  sell when it goes `--y` seconds without a new high **or** drops `--loss-dn` off the peak.

## What's inside

| Module | Role |
| --- | --- |
| `bot.py` | Entry point — wires market/feed/strategy/execution; main loop, console status, JSONL logging |
| `config.py` | `Config` dataclass + argparse (entry/exit thresholds, sizing, risk, `--live`) |
| `market.py` | Resolve a match URL/slug → conditionId + outcome token ids + team names via the public Gamma API (no auth) |
| `feed.py` | `BookFeed` — CLOB market WebSocket, best bid/ask per token, background reader + auto-reconnect |
| `strategy.py` | The two-trigger per-side state machine; emits ENTER / EXIT signals |
| `execution.py` | PAPER fill-sim against the live book; hardened LIVE FOK/FAK + GTC limit orders |
| `manual.py` | Manual keystroke buy/sell terminal for both teams (`[1][2]` buy, `[3][4]` sell) |
| `bracket.py` | Set-and-forget bracket trader: buy at price A, sell at B, optional stop; FOK (taker) or `--limit` (GTC maker), `--repeat` to re-arm |

The order-handling in `execution.py` (`clean_fok_size` GCD/cents sizing, HTTP/1.1 httpx patch +
retried auth, FOK→FOK→FAK escalation, geoblock detection) is **ported from the main engine**
(`quant_bot_5min_part17fix27.py`) — treat it as vendored logic; fix bugs upstream, not here.

## Requirements

- Python 3.9+ (uses `from __future__ import annotations`, dataclasses, f-strings).
- pip: `websocket-client`, `python-dotenv`, `httpx`, and the Polymarket CLOB SDK
  (`py_clob_client` or `py_clob_client_v2`). Everything else is stdlib.
- **LIVE only** (handles real funds): a wallet `PRIVATE_KEY` loaded from a local `.arbVenv`
  (dotenv). Keys are read at runtime and **must never be committed** — `.env*`, `.arbVenv`,
  `*.key`/`*.pem` are gitignored. PAPER mode needs no key.

## Usage

```bash
cd /home/polybot/polymarket-bot
source venv/bin/activate

# PAPER (safe, default) — point at a match by URL or slug:
python -m esports_dip_bot.bot dota2-bb4-lgd-2026-06-07 --stake 1 --x 10 --y 10
python -m esports_dip_bot.bot "https://polymarket.com/esports/dota-2/.../dota2-bb4-lgd-2026-06-07"

# tune the rule
python -m esports_dip_bot.bot <slug> --entry 0.25 --target 0.50 --gain-up 0.02 --loss-dn 0.03

# manual terminal (watch both sides, buy/sell by keystroke)
python -m esports_dip_bot.manual <slug> --stake 1

# bracket trader (buy A / sell B, optional stop; --limit for maker orders, --repeat to re-arm)
python -m esports_dip_bot.bracket <slug> --side LGD --buy 0.25 --sell 0.50 --stop 0.12

# LIVE (real money — needs .arbVenv PRIVATE_KEY, the --live flag, and a typed confirmation)
python -m esports_dip_bot.bot <slug> --live --stake 1
```

Every signal and fill (with the observed spread) is appended to
`esports_dip_bot/logs/<slug>.jsonl` for re-optimizing `--x`/`--y` on the matches you actually trade.

> Private research software. No warranty; trades/handles real funds at your own risk.
