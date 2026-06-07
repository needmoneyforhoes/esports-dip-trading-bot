"""Price-bracket trader for a single Polymarket market.

Paste a URL/slug, pick a side, set a BUY price and a SELL price. It watches the live order
book and automatically:
  - BUYS  when the ask drops to <= --buy   (buy the dip / limit-buy)
  - SELLS when the bid rises to >= --sell   (take profit)
  - optionally SELLS if the bid falls to <= --stop  (stop loss; cuts the ->0 death)
With --repeat it re-arms after each round-trip. Live-updating price display throughout.
PAPER by default; --live places real FOK orders (uses the same hardened placement as the bot).

Run:
  python -m esports_dip_bot.bracket <url-or-slug> --side "LGD Gaming" --buy 0.25 --sell 0.50
  python -m esports_dip_bot.bracket dota2-ty-lgd-2026-06-07 --side LGD --buy 0.25 --sell 0.50 \
         --stop 0.12 --stake 1 --repeat --live
"""
from __future__ import annotations
import argparse
import sys
import time

from .market import resolve_market
from .feed import BookFeed
from .execution import Executor
from .config import Config
from .bot import _make_logger, _confirm_live

HOME = "\033[H"
CLR_EOL = "\033[K"
CLEAR = "\033[2J\033[H"


class BracketTrader:
    """State machine: ARMED -> (ask<=buy) -> HOLDING -> (bid>=sell | bid<=stop) -> ARMED/DONE."""

    def __init__(self, mkt, side, token, ex, cfg, buy_price, sell_price, stop_price, repeat):
        self.mkt = mkt
        self.side = side
        self.token = token
        self.ex = ex
        self.cfg = cfg
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.stop_price = stop_price
        self.repeat = repeat
        self.state = "ARMED"
        self.rounds = 0
        self.msg = f"ARMED — waiting for ask <= {buy_price:.3f} to BUY {side}"

    def step(self, bid, ask, now):
        """Advance one tick given current bid/ask. Returns the (possibly new) state."""
        if self.state == "ARMED":
            if ask is not None and ask <= self.buy_price and not self.ex.has_position(self.side):
                ok = self.ex.enter(self.side, self.token, ask, bid, now)
                if ok:
                    self.state = "HOLDING"
                    self.msg = (f"BOUGHT {self.side} @ {ask:.3f} — "
                                f"waiting for bid >= {self.sell_price:.3f} to SELL"
                                + (f" (stop {self.stop_price:.3f})" if self.stop_price else ""))
                else:
                    self.msg = f"BUY attempt @ {ask:.3f} rejected (cap/min/fill) — still ARMED"
        elif self.state == "HOLDING":
            if bid is not None and bid >= self.sell_price:
                self.ex.exit(self.side, self.token, bid, ask, now, reason="take_profit")
                self.rounds += 1
                self._after_sell(f"SOLD (take-profit) @ {bid:.3f}")
            elif self.stop_price and bid is not None and bid <= self.stop_price:
                self.ex.exit(self.side, self.token, bid, ask, now, reason="stop")
                self.rounds += 1
                self._after_sell(f"SOLD (STOP) @ {bid:.3f}")
        return self.state

    def _after_sell(self, what):
        if self.repeat:
            self.state = "ARMED"
            self.msg = f"{what} — re-ARMED (round {self.rounds}), waiting for ask <= {self.buy_price:.3f}"
        else:
            self.state = "DONE"
            self.msg = f"{what} — DONE (realized ${self.ex.realized:+.3f})"

    def done(self):
        return self.state == "DONE"

    def cancel_open(self):
        pass  # FOK bracket has no resting orders to cancel


class LimitBracketTrader:
    """Resting-limit (GTC maker) version: place a limit BUY that sits in the book at your
    price, wait for it to fill, then place a limit SELL at your target. No spread paid.

    PAPER: a resting BUY fills when the live ask reaches <= buy_price (a seller crosses your
    bid); a resting SELL fills when the live bid reaches >= sell_price. Fills at YOUR price.
    LIVE: posts real GTC orders, polls fills, cancels resting orders on exit.
    States: PLACE_BUY -> REST_BUY -> PLACE_SELL -> REST_SELL -> DONE
    """

    def __init__(self, mkt, side, token, ex, cfg, buy_price, sell_price, stop_price, repeat):
        self.mkt = mkt
        self.side = side
        self.token = token
        self.ex = ex
        self.cfg = cfg
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.stop_price = stop_price
        self.repeat = repeat
        self.state = "PLACE_BUY"
        self.rounds = 0
        self.buy_oid = None
        self.sell_oid = None
        self.shares = 0.0
        self.msg = f"placing resting LIMIT BUY @ {buy_price:.3f}"

    def step(self, bid, ask, now):
        from .execution import _buy_const, _sell_const, clean_fok_size
        if self.state == "PLACE_BUY":
            oid, sh = self.ex.place_gtc(self.token, _buy_const(), self.buy_price,
                                        target_usdc=self.cfg.stake)
            if oid is None:
                self.state = "DONE"
                self.msg = "LIMIT BUY placement failed"
                return self.state
            self.buy_oid = oid
            self.shares = sh or clean_fok_size(self.cfg.stake, self.buy_price)[0]
            self.state = "REST_BUY"
            self.msg = f"resting BUY @ {self.buy_price:.3f} — waiting for ask to reach it"
        elif self.state == "REST_BUY":
            if self.cfg.live:
                filled, avg, matched = self.ex.poll_gtc(self.buy_oid)
                if matched > 0:
                    self.shares = matched
                    self.ex.record_buy(self.side, self.token, matched, avg or self.buy_price, now)
                    self.state = "PLACE_SELL"
                    self.msg = f"BUY filled {matched:.2f}sh @ {avg or self.buy_price:.3f}"
            else:
                if ask is not None and ask <= self.buy_price:   # market reached your resting bid
                    self.ex.record_buy(self.side, self.token, self.shares, self.buy_price,
                                       now, note="limit-paper")
                    self.state = "PLACE_SELL"
                    self.msg = f"BUY filled {self.shares:.2f}sh @ {self.buy_price:.3f}"
        elif self.state == "PLACE_SELL":
            oid, _ = self.ex.place_gtc(self.token, _sell_const(), self.sell_price, shares=self.shares)
            self.sell_oid = oid if oid is not None else "PAPER"
            self.state = "REST_SELL"
            self.msg = f"resting SELL @ {self.sell_price:.3f} — waiting for bid to reach it"
        elif self.state == "REST_SELL":
            if self.stop_price and bid is not None and bid <= self.stop_price:
                self.ex.cancel_gtc(self.sell_oid)
                if self.cfg.live:
                    self.ex.exit(self.side, self.token, bid, ask, now, reason="stop")  # market out
                else:
                    self.ex.record_sell_all(self.side, bid, now, reason="stop")
                self.rounds += 1
                self._after_sell(f"STOPPED @ {bid:.3f}")
                return self.state
            if self.cfg.live:
                filled, avg, matched = self.ex.poll_gtc(self.sell_oid)
                if matched > 0:
                    self.ex.record_sell_all(self.side, avg or self.sell_price, now, reason="take_profit")
                    self.rounds += 1
                    self._after_sell(f"SOLD @ {avg or self.sell_price:.3f}")
            else:
                if bid is not None and bid >= self.sell_price:
                    self.ex.record_sell_all(self.side, self.sell_price, now, reason="take_profit")
                    self.rounds += 1
                    self._after_sell(f"SOLD @ {self.sell_price:.3f}")
        return self.state

    def _after_sell(self, what):
        if self.repeat:
            self.state = "PLACE_BUY"
            self.buy_oid = self.sell_oid = None
            self.shares = 0.0
            self.msg = f"{what} — re-placing LIMIT BUY (round {self.rounds})"
        else:
            self.state = "DONE"
            self.msg = f"{what} — DONE (realized ${self.ex.realized:+.3f})"

    def done(self):
        return self.state == "DONE"

    def cancel_open(self):
        for oid in (self.buy_oid, self.sell_oid):
            if oid and oid != "PAPER":
                self.ex.cancel_gtc(oid)


def _resolve_side(mkt, side_arg):
    s = side_arg.strip().lower()
    if s in ("0", "1"):
        i = int(s)
        return (mkt.team0, mkt.token0) if i == 0 else (mkt.team1, mkt.token1)
    if s in mkt.team0.lower():
        return mkt.team0, mkt.token0
    if s in mkt.team1.lower():
        return mkt.team1, mkt.token1
    return None, None


def _parse(argv=None):
    p = argparse.ArgumentParser(prog="esports_dip_bot.bracket",
                                description="Buy at one price, sell at another, on a live market.")
    p.add_argument("match", help="match URL or slug")
    p.add_argument("--side", required=True, help="team name (substring) or 0/1")
    p.add_argument("--buy", type=float, required=True, help="BUY when ask <= this price")
    p.add_argument("--sell", type=float, required=True, help="SELL when bid >= this price")
    p.add_argument("--stop", type=float, default=0.0, help="optional: SELL if bid <= this (stop-loss)")
    p.add_argument("--stake", type=float, default=1.0, help="USDC per BUY (default 1)")
    p.add_argument("--repeat", action="store_true", help="re-arm and repeat after each round-trip")
    p.add_argument("--limit", action="store_true",
                   help="use resting GTC LIMIT (maker) orders instead of FOK (taker) — no spread paid")
    p.add_argument("--live", action="store_true", help="place REAL orders (default paper)")
    return p.parse_args(argv)


def _render(trader, feed):
    mkt = trader.mkt
    bid, ask, mid, ts = feed.get(trader.token)
    b = f"{bid:.3f}" if bid is not None else "  -  "
    a = f"{ask:.3f}" if ask is not None else "  -  "
    m = f"{mid:.3f}" if mid is not None else "  -  "
    pos = trader.ex.positions.get(trader.side, [])
    if pos:
        sh = sum(p.shares for p in pos)
        avg = sum(p.shares * p.entry_price for p in pos) / sh
        mark = bid if bid is not None else (mid or 0.0)
        upnl = sh * mark - sh * avg
        posstr = f"{sh:.2f}sh @{avg:.3f}  uPnL ${upnl:+.2f}"
    else:
        posstr = "—"
    out = [HOME]
    out.append(f"\033[1m BRACKET — {mkt.title} \033[0m[{trader.cfg.mode}]{CLR_EOL}")
    out.append("=" * 76 + CLR_EOL)
    conn = "WS connected" if feed.connected else "WS connecting…"
    out.append(f"  {conn}    side: {trader.side}    stake ${trader.cfg.stake:.2f}{CLR_EOL}")
    out.append(CLR_EOL)
    out.append(f"  live   bid {b}   ask {a}   mid \033[1m{m}\033[0m{CLR_EOL}")
    stopstr = f"   STOP <= {trader.stop_price:.3f}" if trader.stop_price else ""
    out.append(f"  rules  BUY ask<={trader.buy_price:.3f}   SELL bid>={trader.sell_price:.3f}{stopstr}{CLR_EOL}")
    out.append(f"  state  \033[1m{trader.state}\033[0m   pos {posstr}{CLR_EOL}")
    s = trader.ex.summary()
    out.append(f"  realized PnL: \033[1m${s['realized_pnl']:+.3f}\033[0m   rounds={trader.rounds}"
               + ("  ⚠ GEOBLOCKED" if s['geoblocked'] else "") + CLR_EOL)
    out.append(CLR_EOL)
    out.append(f"  > {trader.msg}{CLR_EOL}")
    out.append("  (Ctrl-C to stop)\033[J")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def main(argv=None):
    a = _parse(argv)
    if a.buy <= 0 or a.sell <= 0 or a.sell <= a.buy:
        print("  ERROR: need 0 < --buy < --sell (buy low, sell high)")
        return 2
    print(f"[bracket] resolving '{a.match}' …")
    try:
        mkt = resolve_market(a.match)
    except Exception as e:
        print(f"  ERROR: {e}")
        return 2
    side, token = _resolve_side(mkt, a.side)
    if side is None:
        print(f"  ERROR: --side '{a.side}' didn't match. Teams are: '{mkt.team0}' or '{mkt.team1}'")
        return 2
    kind = "LIMIT (GTC maker)" if a.limit else "FOK (taker)"
    print(f"  {mkt.title}\n  side={side}  {kind}  BUY {a.buy}  SELL {a.sell}"
          + (f"  STOP {a.stop}" if a.stop else "") + f"  closed={mkt.closed}")

    cfg = Config(match=a.match, stake=a.stake, max_per_side=1, live=a.live)
    if cfg.live and not _confirm_live():
        print("  live not confirmed — exiting.")
        return 1

    log, log_path = _make_logger(mkt.slug + ".bracket")
    log({"ts": time.time(), "event": "bracket_start", "mode": cfg.mode, "side": side,
         "order_type": "LIMIT" if a.limit else "FOK", "buy": a.buy, "sell": a.sell,
         "stop": a.stop, "stake": a.stake, "repeat": a.repeat})
    feed = BookFeed([mkt.token0, mkt.token1]).start()
    ex = Executor(mkt, cfg, log, feed=feed)
    TraderCls = LimitBracketTrader if a.limit else BracketTrader
    trader = TraderCls(mkt, side, token, ex, cfg, a.buy, a.sell, a.stop or 0.0, a.repeat)

    sys.stdout.write(CLEAR)
    try:
        while True:
            bid, ask, mid, ts = feed.get(token)
            trader.step(bid, ask, time.time())
            _render(trader, feed)
            if trader.done():
                time.sleep(0.6)
                _render(trader, feed)
                break
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        trader.cancel_open()        # cancel any resting GTC orders (live limit mode)
        feed.stop()
        s = ex.summary()
        log({"ts": time.time(), "event": "bracket_stop", "summary": s})
        print("\n\n  ── bracket summary ──")
        print(f"  mode={cfg.mode}  rounds={trader.rounds}  buys={s['buys']} sells={s['sells']} "
              f"realized PnL=${s['realized_pnl']:+.3f}")
        if s["open"]:
            print(f"  still-open (unrealized): {s['open']}")
        print(f"  log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
