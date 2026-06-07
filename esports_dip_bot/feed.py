"""Live order-book feed over the Polymarket CLOB WebSocket market channel.

Maintains best bid / best ask per token id. Runs the reader in a background thread and
auto-reconnects. Same endpoint + message shapes the main bot's GT monitor uses.
"""
from __future__ import annotations
import json
import threading
import time

from websocket import create_connection  # websocket-client

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class BookFeed:
    def __init__(self, token_ids):
        self.tokens = [str(t) for t in token_ids]
        # token -> {"bid": float|None, "ask": float|None, "ts": float}
        self.books = {t: {"bid": None, "ask": None, "ts": 0.0} for t in self.tokens}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.connected = False
        self.last_msg_ts = 0.0

    # ---- public API ----
    def start(self):
        self._thread = threading.Thread(target=self._run, name="esports-ws", daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def get(self, token):
        """Return (bid, ask, mid, ts) for a token, or (None, None, None, 0) if not seen yet."""
        with self._lock:
            b = self.books.get(str(token), {})
        bid, ask, ts = b.get("bid"), b.get("ask"), b.get("ts", 0.0)
        mid = None
        if bid is not None and ask is not None:
            mid = round((bid + ask) / 2.0, 4)
        elif bid is not None:
            mid = bid
        elif ask is not None:
            mid = ask
        return bid, ask, mid, ts

    # ---- internals ----
    def _apply_book(self, tid, bids, asks, now):
        best_bid = max((float(b["price"]) for b in bids), default=None) if bids else None
        best_ask = min((float(a["price"]) for a in asks), default=None) if asks else None
        with self._lock:
            rec = self.books.setdefault(tid, {"bid": None, "ask": None, "ts": 0.0})
            if best_bid is not None:
                rec["bid"] = best_bid
            if best_ask is not None:
                rec["ask"] = best_ask
            rec["ts"] = now

    def _apply_price_change(self, pc, now):
        tid = pc.get("asset_id") or pc.get("asset")
        if tid is None:
            return
        tid = str(tid)
        bb = pc.get("best_bid")
        ba = pc.get("best_ask")
        with self._lock:
            rec = self.books.setdefault(tid, {"bid": None, "ask": None, "ts": 0.0})
            if bb is not None:
                rec["bid"] = float(bb)
            if ba is not None:
                rec["ask"] = float(ba)
            rec["ts"] = now

    def _handle(self, msg, now):
        et = msg.get("event_type", "")
        if et == "book":
            tid = str(msg.get("asset_id") or msg.get("market") or "")
            if tid in self.books:
                self._apply_book(tid, msg.get("bids", []), msg.get("asks", []), now)
        elif et == "price_change" or "price_changes" in msg:
            for pc in msg.get("price_changes", []):
                self._apply_price_change(pc, now)
            # some shapes put a single change at top level
            if "asset_id" in msg and "best_bid" in msg:
                self._apply_price_change(msg, now)

    def _run(self):
        backoff = 1.0
        while not self._stop.is_set():
            ws = None
            try:
                ws = create_connection(WS_URL, timeout=15)
                ws.send(json.dumps({"type": "market", "assets_ids": self.tokens}))
                self.connected = True
                backoff = 1.0
                while not self._stop.is_set():
                    raw = ws.recv()
                    now = time.time()
                    self.last_msg_ts = now
                    if not raw:
                        continue
                    try:
                        data = json.loads(raw)
                    except Exception:
                        continue
                    if isinstance(data, list):
                        for m in data:
                            self._handle(m, now)
                    elif isinstance(data, dict):
                        self._handle(data, now)
            except Exception:
                self.connected = False
                if self._stop.is_set():
                    break
                time.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            finally:
                try:
                    if ws is not None:
                        ws.close()
                except Exception:
                    pass
        self.connected = False
