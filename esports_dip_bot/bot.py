"""esports_dip_bot entrypoint — wires market resolution, live book feed, the two-trigger
strategy, and execution together. PAPER by default.

Run:  python -m esports_dip_bot.bot <match-url-or-slug> [options]
"""
from __future__ import annotations
import json
import os
import sys
import time

from .config import parse_args
from .market import resolve_market
from .feed import BookFeed
from .strategy import Strategy
from .execution import Executor

LOG_DIR = os.path.join(os.path.dirname(__file__), "logs")


def _make_logger(slug):
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, f"{slug}.jsonl")
    f = open(path, "a", buffering=1)

    def log(rec):
        try:
            f.write(json.dumps(rec) + "\n")
        except Exception:
            pass
    return log, path


def _confirm_live() -> bool:
    print("\n  ⚠️  LIVE mode will place REAL orders with REAL money.")
    print("      Backtest verdict: this rule is NEGATIVE-EV across the full Dota population.")
    try:
        ans = input("      Type 'I UNDERSTAND' to proceed live: ").strip()
    except EOFError:
        return False
    return ans == "I UNDERSTAND"


def main(argv=None):
    cfg = parse_args(argv)

    print(f"[esports_dip_bot] resolving '{cfg.match}' …")
    try:
        mkt = resolve_market(cfg.match)
    except Exception as e:
        print(f"  ERROR: {e}")
        return 2
    print(f"  {mkt.title}")
    print(f"  {mkt.team0}  (token0)  vs  {mkt.team1}  (token1)   closed={mkt.closed}")
    if mkt.closed:
        print("  NOTE: this market is already resolved — live trading is pointless; "
              "running for replay/sanity only.")

    if cfg.live and not _confirm_live():
        print("  live not confirmed — exiting.")
        return 1

    log, log_path = _make_logger(mkt.slug)
    log({"ts": time.time(), "event": "start", "mode": cfg.mode, "match": mkt.slug,
         "params": cfg.__dict__})

    strat = Strategy(
        entry_threshold=cfg.entry_threshold, min_entry_price=cfg.min_entry_price,
        x_seconds=cfg.x_seconds, gain_up=cfg.gain_up,
        max_entry_price=cfg.max_entry_price, target=cfg.target, y_seconds=cfg.y_seconds,
        loss_dn=cfg.loss_dn,
    )
    feed = BookFeed([mkt.token0, mkt.token1]).start()
    ex = Executor(mkt, cfg, log, feed=feed)

    sides = [(mkt.team0, mkt.token0), (mkt.team1, mkt.token1)]

    print(f"\n  MODE={cfg.mode}  stake=${cfg.stake}  entry<= {cfg.entry_threshold}  "
          f"X={cfg.x_seconds}s gain_up={cfg.gain_up}  target={cfg.target}  "
          f"Y={cfg.y_seconds}s loss_dn={cfg.loss_dn}")
    print(f"  logging -> {log_path}")
    print("  waiting for live book … (Ctrl-C to stop)\n")

    last_status = 0.0
    halted = False
    try:
        while True:
            now = time.time()
            for side, token in sides:
                bid, ask, mid, ts = feed.get(token)
                if mid is None:
                    continue
                # settlement detection: price pinned at the extremes
                if ex.has_position(side):
                    if mid <= 0.02:
                        ex.settle(side, token, 0.0, now)
                        continue
                    if mid >= 0.98:
                        ex.settle(side, token, 1.0, now)
                        continue
                if halted:
                    continue
                sig = strat.on_price(side, now, mid, ex.has_position(side))
                if sig is None:
                    continue
                if sig["action"] == "ENTER":
                    ex.enter(side, token, ask, bid, now)
                elif sig["action"] == "EXIT":
                    ex.exit(side, token, bid, ask, now, reason=sig.get("reason", ""))

            # match loss cap
            if cfg.match_loss_cap and ex.realized <= -abs(cfg.match_loss_cap) and not halted:
                halted = True
                print(f"\n  ⛔ match loss cap hit (realized ${ex.realized:.2f}) — no new entries.\n")
                log({"ts": now, "event": "halt_loss_cap", "realized": round(ex.realized, 4)})

            # console status ~1/sec
            if not cfg.quiet and now - last_status >= 1.0:
                last_status = now
                parts = []
                for side, token in sides:
                    bid, ask, mid, ts = feed.get(token)
                    stt = strat.state_for(side).state
                    pos = ex.open_count(side)
                    mids = f"{mid:.3f}" if mid is not None else "  -  "
                    parts.append(f"{side[:10]:>10} {mids} [{stt}{'•'*pos}]")
                conn = "WS✓" if feed.connected else "WS…"
                sys.stdout.write(
                    f"\r  {conn}  " + "   ".join(parts) +
                    f"   | buys={ex.n_buys} sells={ex.n_sells} PnL=${ex.realized:+.3f}   ")
                sys.stdout.flush()
            time.sleep(0.2)
    except KeyboardInterrupt:
        print("\n\n  stopping …")
    finally:
        feed.stop()
        s = ex.summary()
        log({"ts": time.time(), "event": "stop", "summary": s})
        print("\n  ── session summary ──")
        print(f"  mode={cfg.mode}  buys={s['buys']} sells={s['sells']} "
              f"realized PnL=${s['realized_pnl']:+.3f}")
        if s["open"]:
            print(f"  still-open positions (unrealized): {s['open']}")
        print(f"  full log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
