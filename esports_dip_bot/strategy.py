"""Two-trigger dip-buy / momentum-reversion state machine.

One independent machine per side (team). Fed a stream of price observations; emits ENTER /
EXIT signals. Uses `mid` for the momentum logic; the executor decides actual fill prices
(ask on buy, bid on sell).

States:
  IDLE  -> price hasn't dipped to entry_threshold yet
  WATCH -> dipped; waiting for the fall to stop (no early buy while making new lows)
  HOLD  -> entered; holding to target (NO early stop, by design)
  TRAIL -> reached target; trailing the high, sell when the climb stalls
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class SideState:
    side: str
    state: str = "IDLE"
    lo: float = 0.0          # local low while WATCHing
    lo_ts: float = 0.0       # time of the last new low
    hi: float = 0.0          # running high since entry
    hi_ts: float = 0.0       # time of the last new high
    entry_mid: float = 0.0   # mid at entry (for reference)


@dataclass
class Strategy:
    entry_threshold: float
    min_entry_price: float
    x_seconds: float
    gain_up: float
    max_entry_price: float
    target: float
    y_seconds: float
    loss_dn: float
    sides: dict = field(default_factory=dict)

    def state_for(self, side: str) -> SideState:
        if side not in self.sides:
            self.sides[side] = SideState(side=side)
        return self.sides[side]

    def on_price(self, side: str, ts: float, mid: float, has_position: bool):
        """Advance the state machine for `side`. Returns a signal dict or None.

        has_position: whether the executor currently holds a position on this side.
        Signals: {"action": "ENTER", "side": ...} or {"action": "EXIT", "side": ...,
                  "reason": "trail_timer"|"trail_drop"}
        """
        if mid is None:
            return None
        st = self.state_for(side)

        # If executor has no position but we think we're holding, reset (e.g., after a settle).
        if not has_position and st.state in ("HOLD", "TRAIL"):
            st.state = "IDLE"

        if st.state == "IDLE":
            if not has_position and mid <= self.entry_threshold:
                st.state = "WATCH"
                st.lo = mid
                st.lo_ts = ts
            return None

        if st.state == "WATCH":
            if mid < st.lo:                       # still falling -> reset the clock, wait
                st.lo = mid
                st.lo_ts = ts
                return None
            bounced = mid >= st.lo + self.gain_up          # ticked up off the low
            stable = (ts - st.lo_ts) >= self.x_seconds     # held X seconds without a new low
            if bounced or stable:
                if mid < self.min_entry_price:    # corpse, not a dip -> keep watching, never buy this low
                    return None
                if mid > self.max_entry_price:    # gapped away -> don't chase, give up this dip
                    st.state = "IDLE"
                    return None
                st.entry_mid = mid
                st.hi = mid
                st.hi_ts = ts
                st.state = "TRAIL" if mid >= self.target else "HOLD"
                return {"action": "ENTER", "side": side,
                        "reason": "bounce" if bounced else "stable", "mid": mid}
            return None

        if st.state == "HOLD":                    # no early stop by design; wait for target
            if mid > st.hi:
                st.hi = mid
                st.hi_ts = ts
            if mid >= self.target:
                st.state = "TRAIL"
                st.hi = mid
                st.hi_ts = ts
            return None

        if st.state == "TRAIL":                   # in profit zone; trail the high
            if mid > st.hi:
                st.hi = mid
                st.hi_ts = ts
                return None
            drop = mid <= st.hi - self.loss_dn            # lost loss_dn off the peak
            stalled = (ts - st.hi_ts) >= self.y_seconds   # Y seconds without a new high
            if drop or stalled:
                st.state = "IDLE"
                return {"action": "EXIT", "side": side,
                        "reason": "trail_drop" if drop else "trail_timer", "mid": mid}
            return None

        return None
