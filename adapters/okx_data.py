"""Data layer: one daily brief JSON pulled from OKX market-data + sentiment.

Modes (env var OKX_MODE, or the `mode` argument):
  mock  -> realistic, internally-consistent snapshot with a clear story of the
           day. Deterministic per date (seeded jitter) so a given day's brief
           is stable, while different days produce different numbers/hooks.
  real  -> STUB. No credentials exist yet. `REAL_COMMANDS` below documents the
           exact `okx` CLI (@okx_ai/okx-trade-cli) commands that produce each
           field of the brief; `_real_daily_brief` raises with that plan.

Brief JSON shape (the contract every downstream stage relies on):

{
  "date": "YYYY-MM-DD",
  "as_of_utc": "...Z",
  "mode": "mock" | "real",
  "story_of_the_day": str,
  "majors": {
     "BTC-USDT": {"last": float, "chg24h_pct": float, "high24h": float,
                   "low24h": float, "vol24h_usd": float},
     "ETH-USDT": {...}
  },
  "gainers": [ {"inst": "SOL-USDT", "last": float, "chg24h_pct": float,
                "vol24h_usd": float} x3, sorted desc by chg ],
  "losers":  [ ... x3, sorted asc by chg (worst first) ],
  "funding": [ {"inst": "SOL-USDT-SWAP", "rate_pct": float, "note": str} x3 ],
  "sentiment": {
     "score": float in [-1, 1], "label": str,
     "items": [ {"headline": str, "sentiment": "bullish"|"bearish"|"neutral",
                 "coins": [str], "source": str} x2-3 ]
  }
}
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import random

MODE_ENV = "OKX_MODE"
DEFAULT_MODE = "mock"

# --------------------------------------------------------------------------
# REAL MODE (STUB) — exact commands, verified against the public
# okx/agent-skills repo (each skill's SKILL.md documents its CLI surface).
# Install: npm install -g @okx_ai/okx-trade-cli
# Market data needs NO credentials; news/sentiment needs ~/.okx/config.toml.
# --------------------------------------------------------------------------
REAL_COMMANDS = {
    "majors.BTC": "okx market ticker BTC-USDT --json",
    "majors.ETH": "okx market ticker ETH-USDT --json",
    "gainers": (
        "okx market filter --instType SPOT --quoteCcy USDT "
        "--sortBy chg24hPct --sortOrder desc --minVolUsd24h 10000000 "
        "--limit 3 --json"
    ),
    "losers": (
        "okx market filter --instType SPOT --quoteCcy USDT "
        "--sortBy chg24hPct --sortOrder asc --minVolUsd24h 10000000 "
        "--limit 3 --json"
    ),
    "funding.extremes.high": (
        "okx market filter --instType SWAP --sortBy fundingRate "
        "--sortOrder desc --minOiUsd 10000000 --limit 5 --json"
    ),
    "funding.extremes.low": (
        "okx market filter --instType SWAP --sortBy fundingRate "
        "--sortOrder asc --minOiUsd 10000000 --limit 5 --json"
    ),
    "funding.per_inst": "okx market funding-rate {inst}-USDT-SWAP --json",
    "sentiment.news": "okx news important --limit 5 --json",
    "sentiment.coin": "okx news coin-sentiment --coins BTC,ETH,{movers} --json",
    "sentiment.rank": "okx news sentiment-rank --limit 10 --json",
}


def resolve_mode(mode: str | None = None) -> str:
    return (mode or os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()


def get_daily_brief(mode: str | None = None, date: str | None = None) -> dict:
    """Entry point: return the daily brief JSON for `date` (default: today)."""
    mode = resolve_mode(mode)
    date = date or _dt.date.today().isoformat()
    if mode == "mock":
        return _mock_daily_brief(date)
    if mode == "real":
        return _real_daily_brief(date)
    raise ValueError(f"Unknown OKX_MODE {mode!r} (expected 'mock' or 'real')")


# --------------------------------------------------------------------------
# Mock implementation
# --------------------------------------------------------------------------

# Base snapshot. Story of the day: rotation into majors — SOL rips on record
# ETF inflows, BTC grinds to new local highs on rising OI, memes bleed.
_BASE = {
    "story": "Rotation into majors: SOL rips on record ETF inflows while memes bleed.",
    "majors": {
        "BTC-USDT": {"last": 118442.5, "chg": 2.4, "vol_usd": 1.94e9},
        "ETH-USDT": {"last": 4386.2, "chg": 3.1, "vol_usd": 1.12e9},
    },
    "gainers": [
        {"inst": "SOL-USDT", "last": 232.15, "chg": 11.8, "vol_usd": 8.94e8},
        {"inst": "TON-USDT", "last": 7.84, "chg": 9.4, "vol_usd": 2.10e8},
        {"inst": "LINK-USDT", "last": 26.41, "chg": 7.2, "vol_usd": 1.87e8},
    ],
    "losers": [
        {"inst": "PEPE-USDT", "last": 0.00000892, "chg": -8.6, "vol_usd": 3.42e8},
        {"inst": "APT-USDT", "last": 9.12, "chg": -5.9, "vol_usd": 9.8e7},
        {"inst": "WLD-USDT", "last": 1.87, "chg": -4.1, "vol_usd": 7.6e7},
    ],
    "funding": [
        {"inst": "SOL-USDT-SWAP", "rate_pct": 0.082,
         "note": "roughly 8x baseline - longs crowded"},
        {"inst": "BTC-USDT-SWAP", "rate_pct": 0.021,
         "note": "mildly positive, healthy trend"},
        {"inst": "PEPE-USDT-SWAP", "rate_pct": -0.045,
         "note": "negative - shorts paying to press"},
    ],
    "sentiment": {
        "score": 0.62,
        "label": "risk-on",
        "items": [
            {"headline": "Spot Solana ETF logs record $312M single-day inflow",
             "sentiment": "bullish", "coins": ["SOL"], "source": "news"},
            {"headline": "BTC open interest hits a 3-month high as price clears $118K",
             "sentiment": "bullish", "coins": ["BTC"], "source": "derivatives"},
            {"headline": "Capital rotates out of memes and into majors",
             "sentiment": "bearish", "coins": ["PEPE", "WLD"], "source": "social"},
        ],
    },
}


def _rng_for(date: str, salt: str = "") -> random.Random:
    seed = int(hashlib.sha256(f"{date}|{salt}".encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def _jitter_price(rng: random.Random, price: float) -> float:
    """+-0.5% price jitter, keeping sensible precision."""
    p = price * (1 + rng.uniform(-0.005, 0.005))
    if price >= 1000:
        return round(p, 1)
    if price >= 1:
        return round(p, 2)
    return round(p, 10)


def _jitter_chg(rng: random.Random, chg: float) -> float:
    """+-0.3pp change jitter that can never flip the sign."""
    c = chg + rng.uniform(-0.3, 0.3)
    if chg > 0:
        c = max(c, 0.5)
    else:
        c = min(c, -0.5)
    return round(c, 1)


def _hi_lo(last: float, chg_pct: float) -> tuple[float, float]:
    """Internally consistent 24h high/low around last and the implied open."""
    prev = last / (1 + chg_pct / 100.0)
    hi = max(last, prev) * 1.012
    lo = min(last, prev) * 0.991
    nd = 1 if last >= 1000 else (2 if last >= 1 else 10)
    return round(hi, nd), round(lo, nd)


def _mock_daily_brief(date: str) -> dict:
    rng = _rng_for(date, "data")

    majors = {}
    for inst, m in _BASE["majors"].items():
        last = _jitter_price(rng, m["last"])
        chg = _jitter_chg(rng, m["chg"])
        hi, lo = _hi_lo(last, chg)
        majors[inst] = {
            "last": last, "chg24h_pct": chg, "high24h": hi, "low24h": lo,
            "vol24h_usd": round(m["vol_usd"] * rng.uniform(0.9, 1.15)),
        }

    def movers(rows, reverse):
        out = []
        for r in rows:
            out.append({
                "inst": r["inst"],
                "last": _jitter_price(rng, r["last"]),
                "chg24h_pct": _jitter_chg(rng, r["chg"]),
                "vol24h_usd": round(r["vol_usd"] * rng.uniform(0.85, 1.2)),
            })
        out.sort(key=lambda r: r["chg24h_pct"], reverse=reverse)
        return out

    gainers = movers(_BASE["gainers"], reverse=True)
    losers = movers(_BASE["losers"], reverse=False)  # worst first

    funding = []
    for f in _BASE["funding"]:
        rate = f["rate_pct"] * rng.uniform(0.85, 1.15)
        funding.append({
            "inst": f["inst"],
            "rate_pct": round(rate, 4),
            "note": f["note"],
        })

    sent = _BASE["sentiment"]
    score = round(min(1.0, max(-1.0, sent["score"] + rng.uniform(-0.05, 0.05))), 2)

    return {
        "date": date,
        "as_of_utc": f"{date}T06:00:00Z",
        "mode": "mock",
        "story_of_the_day": _BASE["story"],
        "majors": majors,
        "gainers": gainers,
        "losers": losers,
        "funding": funding,
        "sentiment": {
            "score": score,
            "label": sent["label"],
            "items": [dict(i) for i in sent["items"]],
        },
    }


# --------------------------------------------------------------------------
# Real implementation (stub)
# --------------------------------------------------------------------------

def _real_daily_brief(date: str) -> dict:
    """REAL MODE STUB - wiring plan (no OKX credentials exist yet).

    Once `npm install -g @okx_ai/okx-trade-cli` is done, the brief is built by
    running the commands in REAL_COMMANDS (subprocess.run each with --json and
    json.loads the stdout):

      1. majors    : REAL_COMMANDS['majors.BTC'] / ['majors.ETH']
                     -> last, high24h, low24h, vol24h, chg24h%.
      2. gainers   : REAL_COMMANDS['gainers']  (SPOT screener, USDT quote,
                     min $10M 24h volume so illiquid pairs don't pollute).
      3. losers    : REAL_COMMANDS['losers'].
      4. funding   : REAL_COMMANDS['funding.extremes.high'/'low'] for notable
                     rates, or per-instrument REAL_COMMANDS['funding.per_inst'].
                     Market-data commands need NO API credentials.
      5. sentiment : REAL_COMMANDS['sentiment.news' / 'coin' / 'rank'].
                     Requires OKX API credentials in ~/.okx/config.toml and a
                     LIVE profile (news does not support demo mode).

    Map the CLI JSON into the brief shape documented at the top of this file,
    set story_of_the_day from the top sentiment item + top gainer.
    """
    cmds = "\n  ".join(f"{k}: {v}" for k, v in REAL_COMMANDS.items())
    raise NotImplementedError(
        "OKX_MODE=real is a documented stub - no OKX credentials configured. "
        f"Wire it with these okx-trade-cli commands:\n  {cmds}\n"
        "See adapters/okx_data.py:_real_daily_brief for the field mapping."
    )


# --------------------------------------------------------------------------
# Validation (used by tests and the pipeline)
# --------------------------------------------------------------------------

def validate_brief(brief: dict) -> list[str]:
    """Return a list of consistency problems (empty list == valid)."""
    problems: list[str] = []

    for key in ("date", "as_of_utc", "mode", "story_of_the_day", "majors",
                "gainers", "losers", "funding", "sentiment"):
        if key not in brief:
            problems.append(f"missing key: {key}")
    if problems:
        return problems

    for inst in ("BTC-USDT", "ETH-USDT"):
        m = brief["majors"].get(inst)
        if not m:
            problems.append(f"majors missing {inst}")
            continue
        if not (m["low24h"] <= m["last"] <= m["high24h"]):
            problems.append(f"{inst}: last outside 24h range")
        if m["vol24h_usd"] <= 0:
            problems.append(f"{inst}: non-positive volume")

    g = brief["gainers"]
    if len(g) != 3:
        problems.append("expected 3 gainers")
    if any(r["chg24h_pct"] <= 0 for r in g):
        problems.append("gainer with non-positive 24h change")
    if [r["chg24h_pct"] for r in g] != sorted((r["chg24h_pct"] for r in g), reverse=True):
        problems.append("gainers not sorted desc")

    l = brief["losers"]
    if len(l) != 3:
        problems.append("expected 3 losers")
    if any(r["chg24h_pct"] >= 0 for r in l):
        problems.append("loser with non-negative 24h change")
    if [r["chg24h_pct"] for r in l] != sorted(r["chg24h_pct"] for r in l):
        problems.append("losers not sorted asc (worst first)")

    for f in brief["funding"]:
        if not f["inst"].endswith("-SWAP"):
            problems.append(f"funding inst {f['inst']} is not a -SWAP")
        if abs(f["rate_pct"]) > 3:
            problems.append(f"funding rate {f['rate_pct']} implausible")

    items = brief["sentiment"]["items"]
    if not (2 <= len(items) <= 3):
        problems.append("expected 2-3 sentiment items")
    if not (-1.0 <= brief["sentiment"]["score"] <= 1.0):
        problems.append("sentiment score out of [-1, 1]")

    return problems
