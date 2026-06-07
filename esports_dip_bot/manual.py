"""Manual live trading terminal for a single Polymarket esports market.

Paste a match URL or slug; watch BOTH teams' live bid/ask/mid update in real time; buy and
sell each side manually with single keystrokes. PAPER by default (simulates against the live
book); `--live` places real orders (typed confirmation required).

Hotkeys:
   [1] BUY team0   [2] BUY team1
   [3] SELL team0  [4] SELL team1   (sells the whole side)
   [q] quit

Run:
   python -m esports_dip_bot.manual "https://polymarket.com/esports/dota-2/.../dota2-ty-lgd-2026-06-07"
   python -m esports_dip_bot.manual dota2-ty-lgd-2026-06-07 --stake 2 --live
"""
from __future__ import annotations
import argparse
import select
import sys
import time

from .market import resolve_market
from .feed import BookFeed
from .execution import Executor
from .config import Config
from .bot import _make_logger, _confirm_live

CLEAR = "\033[2J\033[H"
HOME = "\033[H"
CLR_EOL = "\033[K"


class ManualTrader:
    def __init__(self, mkt, feed, ex, cfg):
        self.mkt = mkt
        self.feed = feed
        self.ex = ex
        self.cfg = cfg
        self.sides = [(mkt.team0, mkt.token0), (mkt.team1, mkt.token1)]
        self.msg = "ready"

    # ---- position helpers ----
    def _pos_summary(self, side):
        lots = self.ex.positions.get(side, [])
        if not lots:
            return None
        sh = sum(p.shares for p in lots)
        cost = sum(p.shares * p.entry_price for p in lots)
        avg = cost / sh if sh else 0.0
        return sh, avg

    def _unrealized(self, side, token):
        ps = self._pos_summary(side)
        if not ps:
            return 0.0
        sh, avg = ps
        bid, ask, mid, ts = self.feed.get(token)
        mark = bid if bid is not None else (mid or 0.0)
        return sh * mark - sh * avg

    # ---- actions ----
    def buy(self, idx):
        side, token = self.sides[idx]
        bid, ask, mid, ts = self.feed.get(token)
        if ask is None:
            self.msg = f"can't BUY {side}: no ask yet"
            return
        ok = self.ex.enter(side, token, ask, bid, time.time())
        self.msg = (f"BUY {side} @ {ask:.3f}" if ok else f"BUY {side} rejected (cap/min/fill)")

    def sell(self, idx):
        side, token = self.sides[idx]
        if not self.ex.has_position(side):
            self.msg = f"no {side} position to sell"
            return
        bid, ask, mid, ts = self.feed.get(token)
        if bid is None:
            self.msg = f"can't SELL {side}: no bid yet"
            return
        ok = self.ex.exit(side, token, bid, ask, time.time(), reason="manual")
        self.msg = (f"SELL {side} @ {bid:.3f}" if ok else f"SELL {side} failed")

    # ---- render ----
    def render(self):
        out = [HOME]
        title = f" MANUAL TRADING — {self.mkt.title} "
        mode = self.cfg.mode
        out.append(f"\033[1m{title}\033[0m[{mode}]{CLR_EOL}")
        out.append("=" * 78 + CLR_EOL)
        conn = "WS connected" if self.feed.connected else "WS connecting…"
        out.append(f"  {conn}    stake ${self.cfg.stake:.2f}{CLR_EOL}")
        out.append(CLR_EOL)
        for i, (side, token) in enumerate(self.sides):
            bid, ask, mid, ts = self.feed.get(token)
            b = f"{bid:.3f}" if bid is not None else "  -  "
            a = f"{ask:.3f}" if ask is not None else "  -  "
            m = f"{mid:.3f}" if mid is not None else "  -  "
            ps = self._pos_summary(side)
            if ps:
                sh, avg = ps
                upnl = self._unrealized(side, token)
                posstr = f"pos {sh:.2f}sh @{avg:.3f}  uPnL ${upnl:+.2f}"
            else:
                posstr = "pos —"
            out.append(f"  [{i*2+1}] {side[:16]:<16}  bid {b}  ask {a}  mid \033[1m{m}\033[0m"
                       f"    {posstr}{CLR_EOL}")
        out.append(CLR_EOL)
        s = self.ex.summary()
        out.append(f"  realized PnL: \033[1m${s['realized_pnl']:+.3f}\033[0m   "
                   f"buys={s['buys']} sells={s['sells']}"
                   + ("  ⚠ GEOBLOCKED" if s['geoblocked'] else "") + CLR_EOL)
        out.append(CLR_EOL)
        t0, t1 = self.mkt.team0[:10], self.mkt.team1[:10]
        out.append(f"  keys: [1] buy {t0}  [2] buy {t1}   "
                   f"[3] sell {t0}  [4] sell {t1}   [q] quit{CLR_EOL}")
        out.append(f"  > {self.msg}{CLR_EOL}")
        out.append("\033[J")  # clear below
        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def handle_key(self, c):
        if c in ("q", "Q", "\x03"):  # q or Ctrl-C
            return False
        if c == "1":
            self.buy(0)
        elif c == "2":
            self.buy(1)
        elif c == "3":
            self.sell(0)
        elif c == "4":
            self.sell(1)
        return True


def _parse(argv=None):
    p = argparse.ArgumentParser(prog="esports_dip_bot.manual",
                                description="Manual live buy/sell terminal for one Polymarket market.")
    p.add_argument("match", help="match URL or slug")
    p.add_argument("--stake", type=float, default=1.0, help="USDC per BUY (default 1)")
    p.add_argument("--live", action="store_true", help="place REAL orders (default paper)")
    return p.parse_args(argv)


def main(argv=None):
    a = _parse(argv)
    print(f"[manual] resolving '{a.match}' …")
    try:
        mkt = resolve_market(a.match)
    except Exception as e:
        print(f"  ERROR: {e}")
        return 2
    print(f"  {mkt.title}\n  {mkt.team0} vs {mkt.team1}   closed={mkt.closed}")

    cfg = Config(match=a.match, stake=a.stake, max_per_side=99, live=a.live)
    if cfg.live and not _confirm_live():
        print("  live not confirmed — exiting.")
        return 1

    log, log_path = _make_logger(mkt.slug + ".manual")
    log({"ts": time.time(), "event": "manual_start", "mode": cfg.mode, "stake": cfg.stake})
    feed = BookFeed([mkt.token0, mkt.token1]).start()
    ex = Executor(mkt, cfg, log, feed=feed)
    trader = ManualTrader(mkt, feed, ex, cfg)

    is_tty = sys.stdin.isatty()
    old = None
    try:
        if is_tty:
            import termios
            import tty
            old = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())  # cbreak keeps Ctrl-C working
        sys.stdout.write(CLEAR)
        running = True
        while running:
            trader.render()
            r, _, _ = select.select([sys.stdin], [], [], 0.25)
            if r:
                c = sys.stdin.read(1)
                if c:
                    running = trader.handle_key(c)
    except KeyboardInterrupt:
        pass
    finally:
        if old is not None:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        feed.stop()
        s = ex.summary()
        log({"ts": time.time(), "event": "manual_stop", "summary": s})
        print("\n\n  ── manual session summary ──")
        print(f"  mode={cfg.mode}  buys={s['buys']} sells={s['sells']} "
              f"realized PnL=${s['realized_pnl']:+.3f}")
        if s["open"]:
            print(f"  still-open (unrealized): {s['open']}")
        print(f"  log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
