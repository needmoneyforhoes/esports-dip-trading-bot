"""Configuration + CLI parsing for esports_dip_bot."""
from __future__ import annotations
import argparse
from dataclasses import dataclass


@dataclass
class Config:
    # --- market ---
    match: str                      # URL or slug of the esports match

    # --- entry rule ---
    entry_threshold: float = 0.25   # start watching once a side's price dips to/below this
    min_entry_price: float = 0.10   # floor: never buy below this — it's a dying team, not a dip
    x_seconds: float = 10.0         # ENTER after this many seconds without a new low...
    gain_up: float = 0.02           # ...OR as soon as price ticks this far back up off the low
    max_entry_price: float = 0.33   # never chase: skip the entry if price gapped above this

    # --- exit rule ---
    target: float = 0.50            # only start trailing for profit once price reaches this
    y_seconds: float = 10.0         # SELL after this many seconds without a new high...
    loss_dn: float = 0.03           # ...OR as soon as price drops this far off the peak

    # --- sizing / risk ---
    stake: float = 1.0              # USDC per entry
    max_per_side: int = 1           # max concurrent positions per side (no averaging down)
    match_loss_cap: float = 0.0     # stop the bot for this match after this much realized loss (0 = off)

    # --- mode ---
    live: bool = False              # False = PAPER (simulate vs live book); True = real orders
    quiet: bool = False             # less console chatter

    @property
    def mode(self) -> str:
        return "LIVE" if self.live else "PAPER"


def parse_args(argv=None) -> Config:
    p = argparse.ArgumentParser(
        prog="esports_dip_bot",
        description="Two-trigger dip-buy / momentum-reversion bot for Polymarket esports (PAPER by default).",
    )
    p.add_argument("match", help="Match URL or slug, e.g. dota2-bb4-lgd-2026-06-07")
    p.add_argument("--entry", type=float, default=0.25, dest="entry_threshold",
                   help="dip threshold to start watching (default 0.25)")
    p.add_argument("--min-entry", type=float, default=0.10, dest="min_entry_price",
                   help="floor: never buy below this price — a corpse, not a dip (default 0.10)")
    p.add_argument("--x", type=float, default=10.0, dest="x_seconds",
                   help="entry: seconds stable (no new low) before buying (default 10)")
    p.add_argument("--gain-up", type=float, default=0.02, dest="gain_up",
                   help="entry: cents up off the low that triggers an early buy (default 0.02)")
    p.add_argument("--max-entry", type=float, default=0.33, dest="max_entry_price",
                   help="never chase an entry above this price (default 0.33)")
    p.add_argument("--target", type=float, default=0.50,
                   help="price at which to start trailing for profit (default 0.50)")
    p.add_argument("--y", type=float, default=10.0, dest="y_seconds",
                   help="exit: seconds without a new high before selling (default 10)")
    p.add_argument("--loss-dn", type=float, default=0.03, dest="loss_dn",
                   help="exit: cents down off the peak that triggers a sell (default 0.03)")
    p.add_argument("--stake", type=float, default=1.0, help="USDC per entry (default 1)")
    p.add_argument("--max-per-side", type=int, default=1, dest="max_per_side",
                   help="max concurrent positions per side (default 1)")
    p.add_argument("--match-loss-cap", type=float, default=0.0, dest="match_loss_cap",
                   help="halt this match after this realized loss (0=off)")
    p.add_argument("--live", action="store_true",
                   help="place REAL orders (default False = paper). Requires confirmation.")
    p.add_argument("--quiet", action="store_true", help="less console output")
    a = p.parse_args(argv)
    return Config(
        match=a.match, entry_threshold=a.entry_threshold, min_entry_price=a.min_entry_price,
        x_seconds=a.x_seconds,
        gain_up=a.gain_up, max_entry_price=a.max_entry_price, target=a.target,
        y_seconds=a.y_seconds, loss_dn=a.loss_dn, stake=a.stake,
        max_per_side=a.max_per_side, match_loss_cap=a.match_loss_cap,
        live=a.live, quiet=a.quiet,
    )
