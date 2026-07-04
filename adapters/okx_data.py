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

# OKX public REST — market data needs NO credentials (verified reachable).
_OKX_BASE = os.environ.get("OKX_API_BASE_URL", "https://www.okx.com")
_HTTP_TIMEOUT = float(os.environ.get("OKX_HTTP_TIMEOUT", "8"))
# Swaps we report funding for (majors + SOL), in display order.
_FUNDING_SWAPS = ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP")
# Rank by 24h volume and keep the most-liquid universe for the movers screen,
# rather than an absolute USD cutoff (OKX's ticker volume field scale varies).
_LIQUID_UNIVERSE = 100
# Stable / pegged bases sit at ~0% and must not pollute a gainers/losers screen.
_STABLE_BASES = {
    "USDT", "USDC", "DAI", "TUSD", "USDD", "FDUSD", "USDP", "PYUSD", "GUSD",
    "USDG", "EURT", "EUR", "EURC", "XAUT", "PAXG", "BUSD", "USDE", "USDS",
}


def _http_get_json(path: str) -> dict:
    """GET {base}{path} and return parsed JSON. stdlib only, hard timeout."""
    import json as _json
    import urllib.request

    url = f"{_OKX_BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "market-brief-studio"})
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
        payload = _json.loads(resp.read().decode("utf-8"))
    if str(payload.get("code")) not in ("0", "None"):
        raise RuntimeError(f"OKX API error for {path}: {payload.get('msg')!r}")
    return payload


def _ticker_row(t: dict) -> dict:
    """Map one OKX ticker to {inst, last, chg24h_pct, vol24h_usd}."""
    last = float(t["last"])
    open24h = float(t["open24h"]) or last
    return {
        "inst": t["instId"],
        "last": last,
        "chg24h_pct": round((last - open24h) / open24h * 100, 1),
        # volCcy24h is quote-ccy (USDT ≈ USD) volume for a -USDT pair.
        "vol24h_usd": round(float(t.get("volCcy24h") or 0.0)),
    }


def _fetch_major(inst: str) -> dict:
    t = _http_get_json(f"/api/v5/market/ticker?instId={inst}")["data"][0]
    last = float(t["last"])
    return {
        "last": round(last, 1 if last >= 1000 else 2),
        "chg24h_pct": round((last - (float(t["open24h"]) or last)) / (float(t["open24h"]) or last) * 100, 1),
        "high24h": round(float(t["high24h"]), 1 if last >= 1000 else 2),
        "low24h": round(float(t["low24h"]), 1 if last >= 1000 else 2),
        "vol24h_usd": round(float(t.get("volCcy24h") or 0.0)),
    }


def _fetch_movers() -> tuple[list[dict], list[dict]]:
    """Top 3 gainers / top 3 losers across liquid SPOT -USDT pairs."""
    rows = _http_get_json("/api/v5/market/tickers?instType=SPOT")["data"]
    liquid = []
    for t in rows:
        if not t["instId"].endswith("-USDT"):
            continue
        if t["instId"].split("-")[0] in _STABLE_BASES:  # skip pegged pairs (~0%)
            continue
        try:
            r = _ticker_row(t)
        except (KeyError, ValueError, ZeroDivisionError):
            continue
        if r["last"] > 0:
            liquid.append(r)
    # Keep the most-liquid pairs, then take movers from that universe so a thin
    # pair can't sneak into the top 3.
    universe = sorted(liquid, key=lambda r: r["vol24h_usd"], reverse=True)[:_LIQUID_UNIVERSE]
    # Strict sign so a green/red day can never yield a "loser" that's actually up.
    gainers = sorted((r for r in universe if r["chg24h_pct"] > 0),
                     key=lambda r: r["chg24h_pct"], reverse=True)[:3]
    losers = sorted((r for r in universe if r["chg24h_pct"] < 0),
                    key=lambda r: r["chg24h_pct"])[:3]
    if len(gainers) < 3 or len(losers) < 3:
        raise RuntimeError("not enough up/down liquid pairs for a movers screen")
    return gainers, losers


def _fetch_funding() -> list[dict]:
    out = []
    for inst in _FUNDING_SWAPS:
        try:
            d = _http_get_json(f"/api/v5/public/funding-rate?instId={inst}")["data"][0]
            rate_pct = round(float(d["fundingRate"]) * 100, 4)
        except (KeyError, ValueError, IndexError, RuntimeError):
            continue
        if abs(rate_pct) < 0.005:
            note = "near flat - balanced positioning"
        elif rate_pct > 0.03:
            note = "elevated - longs crowded, paying to hold"
        elif rate_pct > 0:
            note = "mildly positive - healthy trend"
        else:
            note = "negative - shorts paying to press"
        out.append({"inst": inst, "rate_pct": rate_pct, "note": note})
    if not out:
        raise RuntimeError("no funding rates fetched")
    return out


def _sentiment_from_market(majors: dict, gainers: list[dict], losers: list[dict]) -> dict:
    """Derive a sentiment read from the real numbers (no news API / no auth)."""
    avg_major = sum(m["chg24h_pct"] for m in majors.values()) / max(len(majors), 1)
    score = max(-1.0, min(1.0, round(avg_major / 5.0, 2)))
    label = "risk-on" if score >= 0.2 else "risk-off" if score <= -0.2 else "mixed"
    top_g, top_l = gainers[0], losers[0]
    g_sym, l_sym = top_g["inst"].split("-")[0], top_l["inst"].split("-")[0]
    btc = majors.get("BTC-USDT", {}).get("chg24h_pct", 0.0)
    eth = majors.get("ETH-USDT", {}).get("chg24h_pct", 0.0)
    items = [
        {"headline": f"{g_sym} leads the tape, +{top_g['chg24h_pct']}% on the day",
         "sentiment": "bullish", "coins": [g_sym], "source": "market"},
        {"headline": f"{l_sym} lags the market, {top_l['chg24h_pct']}%",
         "sentiment": "bearish", "coins": [l_sym], "source": "market"},
        {"headline": f"Majors: BTC {btc:+.1f}%, ETH {eth:+.1f}% over 24h",
         "sentiment": "bullish" if avg_major > 0 else "bearish" if avg_major < 0 else "neutral",
         "coins": ["BTC", "ETH"], "source": "market"},
    ]
    return {"score": score, "label": label, "items": items}


def _build_real_brief(date: str) -> dict:
    """Assemble a brief from live OKX public market data. Raises on any problem
    (the caller falls back to mock so the endpoint never breaks)."""
    majors = {inst: _fetch_major(inst) for inst in ("BTC-USDT", "ETH-USDT")}
    gainers, losers = _fetch_movers()
    funding = _fetch_funding()
    sentiment = _sentiment_from_market(majors, gainers, losers)

    top_g = gainers[0]
    g_sym = top_g["inst"].split("-")[0]
    btc_dir = "grinds higher" if majors["BTC-USDT"]["chg24h_pct"] >= 0 else "pulls back"
    story = f"{g_sym} leads (+{top_g['chg24h_pct']}%) as BTC {btc_dir}; {sentiment['label']} tape."

    brief = {
        "date": date,
        "as_of_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "real",
        "story_of_the_day": story,
        "majors": majors,
        "gainers": gainers,
        "losers": losers,
        "funding": funding,
        "sentiment": sentiment,
    }
    problems = validate_brief(brief)
    if problems:
        raise RuntimeError(f"real brief failed validation: {problems}")
    return brief


def _real_daily_brief(date: str) -> dict:
    """Live OKX public market data (majors, movers, funding) with sentiment
    derived from the real tape. Needs NO credentials. On ANY failure (network,
    timeout, API error, validation) it falls back to the deterministic mock
    brief so the /api/generate endpoint always returns a valid brief.

    Note: news/sentiment via the OKX `news` module (auth, live-only) is NOT
    wired — see REAL_COMMANDS. Sentiment here is computed from the real
    market numbers instead, which keeps the whole brief credential-free.
    """
    try:
        return _build_real_brief(date)
    except Exception as e:  # noqa: BLE001 - endpoint safety: never propagate
        fallback = _mock_daily_brief(date)
        fallback["real_error"] = f"{type(e).__name__}: {e}"
        return fallback


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
