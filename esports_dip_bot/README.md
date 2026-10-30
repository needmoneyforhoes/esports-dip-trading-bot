# esports_dip_bot

Live dip-buy / momentum-reversion bot for Polymarket esports **series moneyline** markets
(built first for Dota 2). Implements the two-trigger rule:

- **ENTRY**: when a team's price dips to `entry_threshold` (default 0.25), *wait*. Do **not**
  buy while it is still making new lows. Buy when **either** (a) it stops making new lows for
  `X` seconds, **or** (b) it ticks back up `gain_up` cents off the low (the bounce confirms)
  whichever comes first. Never chases above `max_entry_price`.
- **EXIT**: hold (no early stop) until price reaches `target` (default 0.50). Then trail: sell
  when **either** (a) it goes `Y` seconds without a new high, **or** (b) it drops `loss_dn` cents
  off the peak, whichever comes first. Locks profit the instant the climb stalls.

## ⚠️ READ THIS FIRST: the backtest says this is NOT a money-maker

Across **142 resolved Dota matches** (full population, tick-level), this exact rule is
**negative-EV even with zero trading cost** (≈ −0.11 / $1; conservative-friction 95% CI is
entirely below zero). An earlier +0.18/$1 result was **selection bias**. It only used the
top-50 matches by volume (marquee, evenly-matched comeback series). In a *typical* Dota match,
a team that craters to 25¢ is genuinely losing and gets closed out → the dip-buy dies (−100%).

**Therefore this ships in PAPER mode by default.** Use it as a *measurement instrument*: point
it at the specific close/marquee matches you actually want to trade, let it paper-trade with
the **real order book**, and see whether *that subset* makes money with real fills before you
ever risk a cent. Going LIVE requires the explicit `--live` flag and a typed confirmation.

The recommended parameters (`X=10s`, `Y=10s`, `gain_up=0.02`, `loss_dn=0.03`) are the
*least-bad / most-robust* from the grid search. They minimize bleed, they do not turn a
negative edge positive.

## Usage

```bash
cd polymarket-bot
source venv/bin/activate

# PAPER (safe, default) — point at a match by URL or slug:
python -m esports_dip_bot.bot "https://polymarket.com/esports/dota-2/blast-slam/dota2-bb4-lgd-2026-06-07"
python -m esports_dip_bot.bot dota2-bb4-lgd-2026-06-07 --stake 1 --x 10 --y 10

# tune params
python -m esports_dip_bot.bot <slug> --entry 0.25 --target 0.50 --gain-up 0.02 --loss-dn 0.03

# LIVE (real money — requires .arbVenv PRIVATE_KEY + explicit flag + confirmation)
python -m esports_dip_bot.bot <slug> --live --stake 1
```

Every fill, signal, and the realized/observed spread is logged to
`esports_dip_bot/logs/<slug>.jsonl` so we can re-optimize X/Y on the matches you actually trade.

## Files
- `config.py`: parameters + CLI parsing
- `market.py`: resolve a match URL/slug → conditionId + token ids + team names (Gamma API)
- `feed.py`: CLOB WebSocket live order book (best bid/ask per outcome)
- `strategy.py`: the two-trigger entry/exit state machine (per side)
- `execution.py`: PAPER fill-sim against the live book / guarded LIVE FOK orders
- `bot.py`: wires it together, logging, console status
