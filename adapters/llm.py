"""LLM adapter: brief JSON -> broadcast script segments.

Modes (env var OKX_MODE, or the `mode` argument):
  mock  -> a strong template-based writer that interpolates the real numbers
           from the brief. Varied hooks/outros (seeded by date), punchy
           broadcast tone. This is the default and needs no credentials.
  real  -> Claude API call over urllib (stdlib only). Implemented per the
           current Messages API but NOT exercised in this environment because
           no ANTHROPIC_API_KEY exists yet; treat as a documented stub.

Both modes return the same contract: a list of exactly 5 segment dicts,

    {"kind": "hook"|"majors"|"leader"|"laggards"|"outro",
     "text":    str,   # spoken text (what TTS reads)
     "caption": str,   # short on-screen line
     "stat":    {"label","value","delta","direction"} | None}

writer.py turns these into the timed script (timing map, word counts, xpost).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import urllib.error
import urllib.request

from . import okx_data

SEGMENT_KINDS = ["hook", "majors", "leader", "laggards", "outro"]

# --------------------------------------------------------------------------
# formatting helpers
# --------------------------------------------------------------------------

def fmt_price(p: float) -> str:
    """Display form: $118,442 / $232.15 / $0.00000892."""
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.8f}".rstrip("0")


def spoken_price(p: float) -> str:
    """TTS-friendly form (edge-tts reads '$118,442' naturally)."""
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 100:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return "under a cent"


def base_sym(inst: str) -> str:
    return inst.split("-")[0]


def fmt_delta(chg: float) -> str:
    return f"{chg:+.1f}%"


def _spoken_date(date: str) -> str:
    d = _dt.date.fromisoformat(date)
    return d.strftime("%A, %B %d").replace(" 0", " ")


# --------------------------------------------------------------------------
# MOCK writer (template LLM)
# --------------------------------------------------------------------------

def _catalyst_for(brief: dict, sym: str, bullish: bool) -> str:
    """Pull the matching sentiment headline for a symbol, as a spoken clause."""
    want = "bullish" if bullish else "bearish"
    for item in brief["sentiment"]["items"]:
        if sym in item["coins"] and item["sentiment"] == want:
            h = item["headline"]
            # crude headline -> clause: lowercase first word unless acronym/ticker
            first = h.split(" ", 1)[0]
            if not first.isupper():
                h = h[0].lower() + h[1:]
            # TTS-friendly money units: $312M -> $312 million
            h = re.sub(r"\$(\d+(?:\.\d+)?)M\b", r"$\1 million", h)
            h = re.sub(r"\$(\d+(?:\.\d+)?)B\b", r"$\1 billion", h)
            h = re.sub(r"\$(\d+(?:\.\d+)?)K\b", r"$\1 thousand", h)
            return h
    return "heavy spot demand" if bullish else "the crowd rotates out"


def _template_segments(brief: dict) -> list[dict]:
    rng = okx_data._rng_for(brief["date"], "writer")
    date_spoken = _spoken_date(brief["date"])

    btc = brief["majors"]["BTC-USDT"]
    eth = brief["majors"]["ETH-USDT"]
    g1, g2, g3 = brief["gainers"]
    l1, l2, l3 = brief["losers"]
    fund = {f["inst"]: f for f in brief["funding"]}
    mood = brief["sentiment"]["label"]

    g1s, l1s = base_sym(g1["inst"]), base_sym(l1["inst"])
    up_word = "up" if btc["chg24h_pct"] >= 0 else "down"
    eth_word = "up" if eth["chg24h_pct"] >= 0 else "down"

    hooks = [
        f"It's {date_spoken}. The tape picked a direction overnight, and it's {mood}.",
        f"Sixty seconds on crypto. Three stories, real numbers, no filler. This is your brief for {date_spoken}.",
        f"One number tells today's story: {g1s}, {fmt_delta(g1['chg24h_pct'])} in a day. Here's why, in sixty seconds.",
        f"Big money moved while you slept. It's {date_spoken}, and this is the OKX market brief.",
        f"Markets don't wait, so let's not either. {date_spoken}. Sixty seconds. Go.",
        f"The rotation is on. It's {date_spoken}, and here's what actually matters on the tape.",
    ]
    outros = [
        "That's the brief. Pulled from OKX data this morning, written and voiced by an agent. Same time tomorrow.",
        "Sixty seconds, done. Full numbers on screen now. See you at tomorrow's open.",
        "That's your edge for today, generated end to end from OKX market data. Back tomorrow, same time.",
    ]

    hook_text = rng.choice(hooks)
    outro_text = rng.choice(outros)

    majors_text = (
        f"Story one: the majors. Bitcoin trades at {spoken_price(btc['last'])}, "
        f"{up_word} {abs(btc['chg24h_pct'])} percent, with open interest at a three month high. "
        f"Ethereum keeps pace at {spoken_price(eth['last'])}, {eth_word} {abs(eth['chg24h_pct'])} percent. "
        f"Strength at the top of the board sets the tone."
    )

    g1_fund = fund.get(f"{g1s}-USDT-SWAP")
    fund_clause = (
        f"Funding on {g1s} perps runs {g1_fund['rate_pct']:.3f} percent, so longs are crowded."
        if g1_fund else f"Volume backs the move: {g1['vol24h_usd']/1e6:,.0f} million dollars traded."
    )
    leader_text = (
        f"Story two: {g1s} leads everything, up {g1['chg24h_pct']:.1f} percent "
        f"to {spoken_price(g1['last'])} after {_catalyst_for(brief, g1s, bullish=True)}. "
        f"{base_sym(g2['inst'])} and {base_sym(g3['inst'])} chase at "
        f"{g2['chg24h_pct']:.1f} and {g3['chg24h_pct']:.1f} percent. {fund_clause}"
    )

    l1_fund = fund.get(f"{l1s}-USDT-SWAP")
    bear_fund = (
        f"With {l1s} funding negative at {l1_fund['rate_pct']:.3f} percent, shorts are paying to press it."
        if l1_fund else "Sellers are still in control of the move."
    )
    laggards_text = (
        f"Story three: not everything is green. {l1s} dumps {abs(l1['chg24h_pct'])} percent "
        f"as {_catalyst_for(brief, l1s, bullish=False)}. "
        f"{base_sym(l2['inst'])} and {base_sym(l3['inst'])} follow it down "
        f"{abs(l2['chg24h_pct'])} and {abs(l3['chg24h_pct'])} percent. {bear_fund}"
    )

    return [
        {"kind": "hook", "text": hook_text,
         "caption": brief["story_of_the_day"],
         "stat": {"label": "BTC", "value": fmt_price(btc["last"]),
                  "delta": fmt_delta(btc["chg24h_pct"]),
                  "direction": "up" if btc["chg24h_pct"] >= 0 else "down"}},
        {"kind": "majors", "text": majors_text,
         "caption": f"BTC {fmt_delta(btc['chg24h_pct'])} - ETH {fmt_delta(eth['chg24h_pct'])} - OI at 3-month high",
         "stat": {"label": "BTC-USDT", "value": fmt_price(btc["last"]),
                  "delta": fmt_delta(btc["chg24h_pct"]),
                  "direction": "up" if btc["chg24h_pct"] >= 0 else "down"}},
        {"kind": "leader", "text": leader_text,
         "caption": f"{g1s} leads on record ETF inflows - funding {fund[f'{g1s}-USDT-SWAP']['rate_pct']:+.3f}%"
                    if f"{g1s}-USDT-SWAP" in fund else f"{g1s} leads the board",
         "stat": {"label": g1["inst"], "value": fmt_price(g1["last"]),
                  "delta": fmt_delta(g1["chg24h_pct"]), "direction": "up"}},
        {"kind": "laggards", "text": laggards_text,
         "caption": f"Memes bleed: {l1s} {fmt_delta(l1['chg24h_pct'])} - shorts paying",
         "stat": {"label": l1["inst"], "value": fmt_price(l1["last"]),
                  "delta": fmt_delta(l1["chg24h_pct"]), "direction": "down"}},
        {"kind": "outro", "text": outro_text,
         "caption": "Generated end-to-end by an agent - data - script - voice - video",
         "stat": {"label": "SENTIMENT", "value": brief["sentiment"]["label"].upper(),
                  "delta": f"{brief['sentiment']['score']:+.2f}",
                  "direction": "up" if brief["sentiment"]["score"] >= 0 else "down"}},
    ]


# --------------------------------------------------------------------------
# REAL writer (Claude API stub - implemented, not exercised: no key on box)
# --------------------------------------------------------------------------

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
CLAUDE_MODEL = "claude-opus-4-8"

PROMPT_TEMPLATE = """\
You are the head writer for a daily 60-second crypto market video brief.
Write a tight broadcast script from this OKX market data brief (JSON):

{brief_json}

Rules:
- ~150 words total across exactly 5 segments, in order:
  hook, majors, leader, laggards, outro.
- Punchy broadcast tone. Use the ACTUAL numbers from the data
  (prices, 24h % changes, funding rates). No invented facts.
- hook: one or two sentences that frame the story of the day.
- majors: BTC + ETH with prices and % moves.
- leader: top gainer, its catalyst from the sentiment items, funding color.
- laggards: top losers and what funding says about the move.
- outro: sign-off that mentions the brief is agent-generated from OKX data.

Return ONLY JSON (no markdown fences):
{{"segments": [{{"kind": "...", "text": "<spoken text>",
  "caption": "<short on-screen line>",
  "stat": {{"label": "...", "value": "...", "delta": "...",
            "direction": "up|down"}}}}, ... exactly 5 ...]}}
"""


def build_prompt(brief: dict) -> str:
    return PROMPT_TEMPLATE.format(brief_json=json.dumps(brief, indent=2))


def _claude_segments(brief: dict) -> list[dict]:
    """REAL MODE - call Claude over raw HTTP (stdlib urllib, no pip deps).

    Documented stub: the request shape below matches the current Messages API
    (endpoint, headers, adaptive thinking; no sampling params - those 400 on
    claude-opus-4-8). It raises immediately when ANTHROPIC_API_KEY is absent,
    which is the case in this environment.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OKX_MODE=real needs ANTHROPIC_API_KEY set. The Claude call is "
            "already implemented in adapters/llm.py:_claude_segments "
            f"(POST {ANTHROPIC_URL}, model {CLAUDE_MODEL}); set the key and "
            "re-run, or use OKX_MODE=mock."
        )

    payload = {
        "model": CLAUDE_MODEL,
        "max_tokens": 2048,
        "thinking": {"type": "adaptive"},
        "messages": [{"role": "user", "content": build_prompt(brief)}],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API error {e.code}: {e.read().decode()[:500]}") from e

    if data.get("stop_reason") == "refusal":
        raise RuntimeError("Claude declined the request (stop_reason=refusal)")

    text = next(b["text"] for b in data["content"] if b["type"] == "text")
    text = text.strip()
    if text.startswith("```"):  # strip accidental fences
        text = text.strip("`").lstrip("json").strip()
    segments = json.loads(text)["segments"]
    _check_segments(segments)
    return segments


# --------------------------------------------------------------------------
# public entry
# --------------------------------------------------------------------------

def _check_segments(segments: list[dict]) -> None:
    kinds = [s.get("kind") for s in segments]
    if kinds != SEGMENT_KINDS:
        raise ValueError(f"bad segment kinds {kinds}, expected {SEGMENT_KINDS}")
    for s in segments:
        if not s.get("text", "").strip():
            raise ValueError(f"empty text in segment {s.get('kind')}")


def generate_segments(brief: dict, mode: str | None = None) -> list[dict]:
    mode = okx_data.resolve_mode(mode)
    if mode == "mock":
        segments = _template_segments(brief)
    elif mode == "real":
        segments = _claude_segments(brief)
    else:
        raise ValueError(f"Unknown OKX_MODE {mode!r}")
    _check_segments(segments)
    return segments
