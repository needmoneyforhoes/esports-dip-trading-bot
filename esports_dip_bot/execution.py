"""Execution layer: PAPER (simulate fills against the live book) and robust LIVE FOK.

The LIVE path ports the proven order handling from the main bot (quant_bot_5min_part17fix27.py):
  - _clean_fok_size: GCD/cents sizing so shares*price is a clean 2dp value >= $1 (no API reject)
  - HTTP/1.1 httpx patch + retried auth (the ConnectionTerminated-on-auth fix)
  - submit -> poll get_order to a TERMINAL status -> parse matched/avg
  - geoblock (403) detection -> stop trying live
  - retry escalation: FOK (fresh price) -> FOK (fresher) -> FAK last-resort

PAPER is the default and risks nothing.
"""
from __future__ import annotations
import math
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN

# ── constants (mirror the main bot) ──────────────────────────────────────────
FOK_MIN_ORDER_USDC = 1.0     # Polymarket minimum marketable order
TERMINAL = ("MATCHED", "FILLED", "CANCELLED", "UNMATCHED")


def clean_fok_size(target_usdc: float, price: float,
                   min_usdc: float = FOK_MIN_ORDER_USDC, max_usdc: float = None):
    """Return (shares 4dp, actual_usdc 2dp) s.t. shares*price is a clean cent value >= min_usdc.
    Ported verbatim from the main bot. Returns (0,0) if the $1-min step-up would breach max_usdc."""
    if max_usdc is None:
        max_usdc = max(target_usdc, min_usdc) + 1.0
    price_cents = int(round(price * 100))
    target_cents = int(round(target_usdc * 100))
    if price_cents <= 0 or target_cents <= 0:
        return 0.0, 0.0
    g = math.gcd(price_cents, 10_000)
    step = 10_000 // g
    max_units = (target_cents * 10_000) // price_cents
    aligned = (max_units // step) * step
    if min_usdc > 0:
        min_cents = int(math.ceil(min_usdc * 100))
        if (aligned * price_cents) // 10_000 < min_cents:
            aligned += step
    max_cents = int(math.floor(max_usdc * 100))
    if (aligned * price_cents) // 10_000 > max_cents:
        return 0.0, 0.0
    if aligned <= 0:
        return 0.0, 0.0
    shares_d = Decimal(aligned) / Decimal(10_000)
    actual_cents = (aligned * price_cents) // 10_000
    actual_d = Decimal(actual_cents) / Decimal(100)
    return (float(shares_d.quantize(Decimal('0.0001'), rounding=ROUND_DOWN)),
            float(actual_d.quantize(Decimal('0.01'), rounding=ROUND_DOWN)))


@dataclass
class Position:
    side: str
    token: str
    shares: float
    entry_price: float
    entry_ts: float


@dataclass
class Fill:
    ts: float
    side: str
    action: str          # BUY / SELL / SETTLE
    price: float
    shares: float
    note: str = ""


class Executor:
    def __init__(self, market, cfg, log_fn, feed=None):
        self.market = market
        self.cfg = cfg
        self.log = log_fn
        self.feed = feed                 # used to re-read fresh prices on live retries
        self.positions = {}              # side -> list[Position]
        self.fills = []
        self.realized = 0.0
        self.n_buys = 0
        self.n_sells = 0
        self._clob = None
        self.geoblocked = False

    # ---- queries ----
    def has_position(self, side):
        return bool(self.positions.get(side))

    def open_count(self, side):
        return len(self.positions.get(side, []))

    # ---- live client (lazy, with HTTP/1.1 patch + retried auth) ----
    def _ensure_clob(self):
        if self._clob is not None:
            return self._clob
        import os
        from dotenv import load_dotenv
        load_dotenv(".arbVenv")
        pk = os.getenv("PRIVATE_KEY", "")
        if not pk or len(pk) < 60:
            raise RuntimeError("LIVE mode needs a valid PRIVATE_KEY in .arbVenv")
        try:
            from py_clob_client_v2.client import ClobClient
        except Exception:
            from py_clob_client.client import ClobClient
        # Patch SDK httpx to HTTP/1.1 BEFORE auth (fixes ConnectionTerminated on create_api_key)
        for modname in ("py_clob_client_v2.http_helpers", "py_clob_client.http_helpers"):
            try:
                import importlib, httpx as _httpx
                helpers = importlib.import_module(modname).helpers
                helpers._http_client = _httpx.Client(
                    http2=False, timeout=_httpx.Timeout(3.0, connect=1.0))
                break
            except Exception:
                continue
        last_err = None
        for attempt in range(3):
            try:
                c = ClobClient(host="https://clob.polymarket.com", key=pk, chain_id=137)
                c.set_api_creds(c.create_or_derive_api_key())
                self._clob = c
                self.log({"ts": time.time(), "event": "clob_auth_ok", "attempt": attempt + 1})
                return c
            except Exception as e:
                last_err = e
                self.log({"ts": time.time(), "event": "clob_auth_retry",
                          "attempt": attempt + 1, "err": str(e)[:120]})
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"CLOB auth failed after 3 attempts: {last_err}")

    def _submit(self, token, side_const, limit_price, shares, use_fak):
        """Submit one FOK/FAK order; poll to terminal; return (filled, avg_price, matched_shares).
        Raises on geoblock so the caller can stop trying live."""
        c = self._ensure_clob()
        try:
            from py_clob_client_v2.clob_types import OrderArgs, OrderType
        except Exception:
            from py_clob_client.clob_types import OrderArgs, OrderType
        try:
            order = c.create_order(OrderArgs(
                token_id=token, price=round(limit_price, 2), size=round(shares, 4), side=side_const))
            resp = c.post_order(order, OrderType.FAK if use_fak else OrderType.FOK)
        except Exception as e:
            es = str(e)
            if ("403" in es and "Trading restricted" in es) or "geoblock" in es.lower():
                self.geoblocked = True
                raise RuntimeError("geoblocked")
            self.log({"ts": time.time(), "event": "order_exception", "err": es[:160]})
            return False, 0.0, 0.0
        oid = (resp or {}).get("orderID") or (resp or {}).get("id")
        if not oid:
            self.log({"ts": time.time(), "event": "no_order_id", "resp": str(resp)[:160]})
            return False, 0.0, 0.0
        o = resp if (resp.get("status") in TERMINAL) else {}
        # poll up to ~1s for terminal status
        if o.get("status") not in TERMINAL:
            misses = 0
            for _ in range(20):
                time.sleep(0.05)
                try:
                    r = c.get_order(oid)
                except Exception:
                    r = None
                if r is None:
                    misses += 1
                    if misses >= 6:
                        break
                    continue
                misses = 0
                o = r
                if o.get("status") in TERMINAL:
                    break
        matched = float(o.get("size_matched", 0) or 0)
        avg = float(o.get("average_price", 0) or 0) or round(limit_price, 2)
        status = o.get("status", "")
        filled = matched >= shares * 0.99 or status == "MATCHED"
        return (filled or matched > 0), avg, matched

    def _live_fill(self, token, side_const, get_price, target_usdc):
        """Retry escalation: FOK (fresh price) x2 then FAK. get_price()->(limit, ok).
        Returns (matched_shares, avg_price) — (0,0) on total failure."""
        if self.geoblocked:
            return 0.0, 0.0
        attempts = [(False, 0.0), (False, 0.0), (True, 0.01)]   # (use_fak, extra_aggression)
        for use_fak, bump in attempts:
            limit, ok = get_price()
            if not ok or limit is None:
                time.sleep(0.1)
                continue
            # buys cross up, sells cross down on the aggressive/FAK attempt
            if side_const_is_buy(side_const):
                limit = min(round(limit + bump, 2), 0.99)
            else:
                limit = max(round(limit - bump, 2), 0.01)
            shares, _ = clean_fok_size(target_usdc, limit, max_usdc=target_usdc + 1.0)
            if shares <= 0:
                continue
            try:
                ok2, avg, matched = self._submit(token, side_const, limit, shares, use_fak)
            except RuntimeError as e:
                if str(e) == "geoblocked":
                    self.log({"ts": time.time(), "event": "geoblocked_stop"})
                    return 0.0, 0.0
                raise
            if ok2 and matched > 0:
                return matched, (avg if avg > 0 else limit)
            time.sleep(0.1)
        return 0.0, 0.0

    # ---- actions ----
    def enter(self, side, token, ask, bid, ts):
        if self.open_count(side) >= self.cfg.max_per_side:
            return False
        if not self.cfg.live:
            if ask is None or ask <= 0:
                return False
            shares, cost = clean_fok_size(self.cfg.stake, round(ask, 2), max_usdc=self.cfg.stake + 1.0)
            if shares <= 0:
                shares = round(self.cfg.stake / ask, 2)
            fill_price = ask
        else:
            BUY = _buy_const()
            def getp():
                b, a, m, t = self.feed.get(token) if self.feed else (bid, ask, None, ts)
                return (round(a, 2) if a else round(ask, 2) if ask else None), True
            matched, fill_price = self._live_fill(token, BUY, getp, self.cfg.stake)
            if matched <= 0:
                self.log({"ts": ts, "event": "buy_failed", "side": side, "ask": ask})
                return False
            shares = matched
        self.positions.setdefault(side, []).append(
            Position(side=side, token=token, shares=shares, entry_price=fill_price, entry_ts=ts))
        self.n_buys += 1
        self.fills.append(Fill(ts, side, "BUY", fill_price, shares))
        self.log({"ts": ts, "event": "BUY", "side": side, "price": round(fill_price, 4),
                  "shares": round(shares, 4), "ask": ask, "bid": bid, "mode": self.cfg.mode})
        return True

    def exit(self, side, token, bid, ask, ts, reason=""):
        lots = self.positions.get(side, [])
        if not lots or bid is None:
            return False
        total_pnl = 0.0
        remaining = []
        for pos in lots:
            if not self.cfg.live:
                fill_price = bid
                got = pos.shares
            else:
                SELL = _sell_const()
                def getp():
                    b, a, m, t = self.feed.get(token) if self.feed else (bid, ask, None, ts)
                    return (round(b, 2) if b else round(bid, 2) if bid else None), True
                got, fill_price = self._live_fill(token, SELL, getp, max(pos.shares * bid, FOK_MIN_ORDER_USDC))
                if got <= 0:
                    self.log({"ts": ts, "event": "sell_failed", "side": side, "bid": bid})
                    remaining.append(pos)             # keep the lot; try again next signal/settle
                    continue
                if got < pos.shares * 0.99:            # partial fill — keep the remainder
                    remaining.append(Position(side, token, round(pos.shares - got, 4),
                                              pos.entry_price, pos.entry_ts))
            pnl = got * fill_price - got * pos.entry_price
            total_pnl += pnl
            self.n_sells += 1
            self.fills.append(Fill(ts, side, "SELL", fill_price, got, reason))
            self.log({"ts": ts, "event": "SELL", "side": side, "price": round(fill_price, 4),
                      "entry": round(pos.entry_price, 4), "shares": round(got, 4),
                      "pnl": round(pnl, 4), "reason": reason, "bid": bid, "ask": ask,
                      "mode": self.cfg.mode})
        self.realized += total_pnl
        self.positions[side] = remaining
        return True

    def settle(self, side, token, final_price, ts):
        lots = self.positions.get(side, [])
        if not lots:
            return
        for pos in lots:
            pnl = pos.shares * final_price - pos.shares * pos.entry_price
            self.realized += pnl
            self.fills.append(Fill(ts, side, "SETTLE", final_price, pos.shares))
            self.log({"ts": ts, "event": "SETTLE", "side": side, "final": final_price,
                      "entry": round(pos.entry_price, 4), "shares": round(pos.shares, 4),
                      "pnl": round(pnl, 4)})
        self.positions[side] = []

    # ---- limit (GTC maker) primitives — for the limit bracket ----
    def place_gtc(self, token, side_const, price, target_usdc=None, shares=None):
        """Post a resting GTC limit order. Returns (oid, shares). PAPER returns ('PAPER', shares)."""
        if shares is None:
            tgt = target_usdc or self.cfg.stake
            shares, _ = clean_fok_size(tgt, round(price, 2), max_usdc=tgt + 1.0)
        if shares <= 0:
            return (None, 0.0)
        if not self.cfg.live:
            return ("PAPER", shares)
        if self.geoblocked:
            return (None, 0.0)
        try:
            c = self._ensure_clob()
            try:
                from py_clob_client_v2.clob_types import OrderArgs, OrderType
            except Exception:
                from py_clob_client.clob_types import OrderArgs, OrderType
            order = c.create_order(OrderArgs(
                token_id=token, price=round(price, 2), size=round(shares, 4), side=side_const))
            resp = c.post_order(order, OrderType.GTC)
            oid = (resp or {}).get("orderID") or (resp or {}).get("id")
            self.log({"ts": time.time(), "event": "GTC_PLACED", "price": round(price, 2),
                      "shares": round(shares, 4), "oid": (oid or "")[:24]})
            return (oid, shares) if oid else (None, 0.0)
        except Exception as e:
            es = str(e)
            if ("403" in es and "Trading restricted" in es) or "geoblock" in es.lower():
                self.geoblocked = True
            self.log({"ts": time.time(), "event": "GTC_ERROR", "err": es[:160]})
            return (None, 0.0)

    def poll_gtc(self, oid, timeout=0.2):
        """Poll a resting GTC order. Returns (filled, avg_price, size_matched)."""
        if not self.cfg.live or oid in (None, "PAPER"):
            return (False, 0.0, 0.0)
        c = self._ensure_clob()
        o = {}
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = c.get_order(oid)
            except Exception:
                r = None
            if r:
                o = r
                if o.get("status") in TERMINAL:
                    break
            time.sleep(0.05)
        matched = float(o.get("size_matched", 0) or 0)
        avg = float(o.get("average_price", 0) or 0)
        return (matched > 0 or o.get("status") == "MATCHED", avg, matched)

    def cancel_gtc(self, oid):
        if not self.cfg.live or oid in (None, "PAPER"):
            return True
        try:
            self._ensure_clob().cancel_order(oid)
            self.log({"ts": time.time(), "event": "GTC_CANCELLED", "oid": str(oid)[:24]})
            return True
        except Exception as e:
            self.log({"ts": time.time(), "event": "GTC_CANCEL_FAIL", "err": str(e)[:120]})
            return False

    def record_buy(self, side, token, shares, price, ts, note="limit"):
        """Record a filled BUY (used by the limit bracket for paper + post-GTC-fill bookkeeping)."""
        self.positions.setdefault(side, []).append(Position(side, token, shares, price, ts))
        self.n_buys += 1
        self.fills.append(Fill(ts, side, "BUY", price, shares, note))
        self.log({"ts": ts, "event": "BUY", "side": side, "price": round(price, 4),
                  "shares": round(shares, 4), "note": note, "mode": self.cfg.mode})

    def record_sell_all(self, side, price, ts, reason="limit"):
        """Record a SELL of the whole side at `price`. Returns realized pnl for this sell."""
        lots = self.positions.get(side, [])
        pnl = 0.0
        for p in lots:
            lot_pnl = p.shares * price - p.shares * p.entry_price
            pnl += lot_pnl
            self.n_sells += 1
            self.fills.append(Fill(ts, side, "SELL", price, p.shares, reason))
            self.log({"ts": ts, "event": "SELL", "side": side, "price": round(price, 4),
                      "entry": round(p.entry_price, 4), "shares": round(p.shares, 4),
                      "pnl": round(lot_pnl, 4), "reason": reason, "mode": self.cfg.mode})
        self.realized += pnl
        self.positions[side] = []
        return pnl

    def summary(self):
        return {"buys": self.n_buys, "sells": self.n_sells,
                "realized_pnl": round(self.realized, 4),
                "geoblocked": self.geoblocked,
                "open": {s: len(v) for s, v in self.positions.items() if v}}


# ── BUY/SELL constant helpers (lazy, package-agnostic) ───────────────────────
_BUY = _SELL = None


def _load_consts():
    global _BUY, _SELL
    if _BUY is None:
        try:
            from py_clob_client_v2.order_builder.constants import BUY, SELL
        except Exception:
            from py_clob_client.order_builder.constants import BUY, SELL
        _BUY, _SELL = BUY, SELL


def _buy_const():
    _load_consts()
    return _BUY


def _sell_const():
    _load_consts()
    return _SELL


def side_const_is_buy(side_const):
    _load_consts()
    return side_const == _BUY
