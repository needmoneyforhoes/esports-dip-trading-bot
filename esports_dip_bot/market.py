"""Resolve a Polymarket esports match (URL or slug) to its series-moneyline market.

Returns the conditionId, the two outcome token ids, and the team names, by querying the
public Gamma API. No auth required.
"""
from __future__ import annotations
import json
import urllib.parse
import urllib.request
from dataclasses import dataclass

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class Market:
    slug: str
    title: str
    cond_id: str
    token0: str
    token1: str
    team0: str
    team1: str
    closed: bool

    def token_for(self, side: str) -> str:
        return self.token0 if side == self.team0 else self.token1

    def side_for(self, token: str) -> str:
        return self.team0 if token == self.token0 else self.team1


def _get(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "esports_dip_bot/1.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def _slug_from(match: str) -> str:
    """Accept a full polymarket.com URL or a bare slug; return the event slug."""
    match = match.strip()
    if match.startswith("http"):
        path = urllib.parse.urlparse(match).path
        # last non-empty path segment is the event slug
        segs = [s for s in path.split("/") if s]
        return segs[-1] if segs else match
    return match


def _parse(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except Exception:
            return None
    return x


def resolve_market(match: str) -> Market:
    """Find the 'Match Winner' (series moneyline) market for the given match."""
    slug = _slug_from(match)
    events = _get(f"{GAMMA_API}/events?slug={urllib.parse.quote(slug)}")
    if not events:
        raise ValueError(f"No Polymarket event found for slug '{slug}'")
    ev = events[0]
    title = ev.get("title", slug)
    chosen = None
    for m in ev.get("markets", []):
        gi = (m.get("groupItemTitle") or "")
        q = (m.get("question") or "")
        outs = _parse(m.get("outcomes"))
        toks = _parse(m.get("clobTokenIds"))
        if not (outs and toks and len(outs) == 2):
            continue
        if "Over" in outs or "Yes" in outs:
            continue
        # series moneyline: groupItemTitle == 'Match Winner', or question == the event title
        if gi == "Match Winner" or q.strip() == title.strip():
            chosen = (m, outs, toks)
            break
    if chosen is None:
        raise ValueError(f"No series 'Match Winner' market found in event '{slug}'")
    m, outs, toks = chosen
    return Market(
        slug=slug, title=title, cond_id=m.get("conditionId"),
        token0=str(toks[0]), token1=str(toks[1]),
        team0=str(outs[0]), team1=str(outs[1]),
        closed=bool(m.get("closed")),
    )
