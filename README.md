# esports-dip-trading-bot

Live dip-buy / momentum-reversion bot for Polymarket esports series-moneyline (Dota 2) markets. Paper-trades by default.

Backtest on the full 142-match Dota population is negative-EV even at zero cost (approx -0.11/$1). The earlier +0.18/$1 was top-50-by-volume selection bias. Ships PAPER-first so you can measure whether a specific subset of close/marquee matches is tradable before risking funds.

## Strategy

Two triggers, run as an independent per-side state machine (`IDLE -> WATCH -> HOLD -> TRAIL`):

- Entry: once a side dips to `--entry` (0.25), wait. Never buy while it is still making new lows. Buy when it holds `--x` seconds without a new low, or bounces `--gain-up` off the low. Floored by `--min-entry` (0.10, skip dying teams), never chases above `--max-entry` (0.33).
- Exit: hold with no early stop until price reaches `--target` (0.50), then trail the high. Sell when it goes `--y` seconds without a new high, or drops `--loss-dn` off the peak.

## Contents

- `bot.py`: entry point. Wires market/feed/strategy/execution; main loop, console status, JSONL logging.
- `config.py`: `Config` dataclass + argparse (entry/exit thresholds, sizing, risk, `--live`).
- `market.py`: resolves a match URL/slug to conditionId, outcome token ids, and team names via the public Gamma API (no auth).
- `feed.py`: `BookFeed`. CLOB market WebSocket, best bid/ask per token, background reader with auto-reconnect.
- `strategy.py`: the two-trigger per-side state machine. Emits ENTER / EXIT signals.
- `execution.py`: PAPER fill-sim against the live book; LIVE FOK/FAK + GTC limit orders.
- `manual.py`: keystroke buy/sell terminal for both teams (`[1][2]` buy, `[3][4]` sell).
- `bracket.py`: bracket trader. Buy at A, sell at B, optional stop; FOK taker or `--limit` GTC maker, `--repeat` to re-arm.

Order handling in `execution.py` (`clean_fok_size` GCD/cents sizing, HTTP/1.1 httpx patch + retried auth, FOK->FOK->FAK escalation, geoblock detection) is vendored from `quant_bot_5min_part17fix27.py`. Fix bugs upstream, not here.

## Requirements

Python 3.9+. pip: `websocket-client`, `python-dotenv`, `httpx`, and the Polymarket CLOB SDK (`py_clob_client` or `py_clob_client_v2`). Rest is stdlib.

## Usage

```bash
source venv/bin/activate

# PAPER (default), by match URL or slug:
python -m esports_dip_bot.bot dota2-bb4-lgd-2026-06-07 --stake 1 --x 10 --y 10

# tune the rule
python -m esports_dip_bot.bot <slug> --entry 0.25 --target 0.50 --gain-up 0.02 --loss-dn 0.03

# manual terminal (both sides, buy/sell by keystroke)
python -m esports_dip_bot.manual <slug> --stake 1

# bracket trader (buy A / sell B, optional stop; --limit for maker, --repeat to re-arm)
python -m esports_dip_bot.bracket <slug> --side LGD --buy 0.25 --sell 0.50 --stop 0.12

# LIVE (real funds): requires --live, the .arbVenv key, and a typed confirmation
python -m esports_dip_bot.bot <slug> --live --stake 1
```

Every signal and fill (with observed spread) is appended to `esports_dip_bot/logs/<slug>.jsonl` for re-tuning `--x`/`--y`.

LIVE handles real funds. Loads `PRIVATE_KEY` from `.arbVenv` (dotenv) at runtime; never committed. PAPER needs no key.

## Feedback

Suggestions, improvements, or something not working? Please
[open an issue](https://github.com/needmoneyforhoes/esports-dip-trading-bot/issues/new). Pull requests are welcome too.
