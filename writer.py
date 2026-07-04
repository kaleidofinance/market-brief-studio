"""Script writer: brief JSON -> timed ~150-word broadcast script.

Takes the daily brief, gets 5 segments from the LLM adapter (mock template
writer by default, Claude in real mode), then computes the per-segment timing
map used for captions/slides, plus the <=280-char X post.

Script dict contract:

{
  "date": "YYYY-MM-DD",
  "mode": "mock" | "real",
  "story_of_the_day": str,
  "wps": 2.6,                      # broadcast pace, words/sec (~156 wpm)
  "word_count": int,               # target ~150 (accept 120-185)
  "est_total_seconds": float,
  "full_text": str,               # what the TTS reads, all segments joined
  "segments": [
     {"idx", "kind", "text", "caption", "stat",
      "words": int, "duration": float, "start": float, "end": float} x5
  ],
  "xpost": str                     # <=280 chars, contains #okxai
}
"""

from __future__ import annotations

from adapters import llm, okx_data

# Pace calibrated against the actual TTS voice (edge-tts en-US-GuyNeural):
# a 149-word script measured 76.5s -> ~2.0 words/sec including sentence pauses.
# The storyboard additionally rescales its map to the real mp3 duration in JS.
WPS = 2.0          # words per second, measured for en-US-GuyNeural
SEG_PAUSE = 0.3    # extra breathing room between segments
MIN_SEG_SECONDS = 2.0
WORD_TARGET = (120, 185)     # ~150 nominal per the product brief
RUNTIME_BOUNDS = (50, 90)    # sanity window for the estimated runtime (sec)


def _word_count(text: str) -> int:
    return len(text.split())


def build_timing_map(segments: list[dict]) -> list[dict]:
    """Attach words/duration/start/end to each segment (contiguous timeline)."""
    timed = []
    cursor = 0.0
    for idx, seg in enumerate(segments):
        words = _word_count(seg["text"])
        duration = round(max(MIN_SEG_SECONDS, words / WPS + SEG_PAUSE), 1)
        timed.append({
            "idx": idx,
            "kind": seg["kind"],
            "text": seg["text"],
            "caption": seg.get("caption", ""),
            "stat": seg.get("stat"),
            "words": words,
            "duration": duration,
            "start": round(cursor, 1),
            "end": round(cursor + duration, 1),
        })
        cursor = round(cursor + duration, 1)
    return timed


def compose_xpost(brief: dict, max_len: int = 280) -> str:
    """<=280-char X post with the day's actual numbers and #okxai."""
    btc = brief["majors"]["BTC-USDT"]
    eth = brief["majors"]["ETH-USDT"]
    g1 = brief["gainers"][0]
    l1 = brief["losers"][0]
    g1s, l1s = llm.base_sym(g1["inst"]), llm.base_sym(l1["inst"])

    story = brief["story_of_the_day"].rstrip(".")
    post = (
        f"Daily Market Brief - {brief['date']}\n"
        f"BTC {llm.fmt_price(btc['last'])} ({llm.fmt_delta(btc['chg24h_pct'])}) | "
        f"ETH {llm.fmt_delta(eth['chg24h_pct'])}\n"
        f"{g1s} {llm.fmt_delta(g1['chg24h_pct'])} leads; {l1s} {llm.fmt_delta(l1['chg24h_pct'])} bleeds.\n"
        f"{story}.\n"
        f"60-second AI voice brief below. #okxai"
    )
    if len(post) > max_len:  # drop the story line first, then hard-trim
        post = post.replace(f"{story}.\n", "")
    if len(post) > max_len:
        keep = "\n60-second AI voice brief below. #okxai"
        post = post[: max_len - len(keep) - 1].rstrip() + keep
    return post


def write_script(brief: dict, mode: str | None = None) -> dict:
    mode = okx_data.resolve_mode(mode)
    segments = llm.generate_segments(brief, mode)
    timed = build_timing_map(segments)
    full_text = " ".join(s["text"] for s in timed)
    return {
        "date": brief["date"],
        "mode": mode,
        "story_of_the_day": brief["story_of_the_day"],
        "wps": WPS,
        "word_count": _word_count(full_text),
        "est_total_seconds": timed[-1]["end"],
        "full_text": full_text,
        "segments": timed,
        "xpost": compose_xpost(brief),
    }


def validate_script(script: dict) -> list[str]:
    """Return a list of problems (empty == valid)."""
    problems: list[str] = []
    segs = script["segments"]

    kinds = [s["kind"] for s in segs]
    if kinds != llm.SEGMENT_KINDS:
        problems.append(f"segment kinds {kinds} != {llm.SEGMENT_KINDS}")

    lo, hi = WORD_TARGET
    if not (lo <= script["word_count"] <= hi):
        problems.append(f"word count {script['word_count']} outside {WORD_TARGET}")

    if segs and segs[0]["start"] != 0.0:
        problems.append("timeline does not start at 0")
    for prev, nxt in zip(segs, segs[1:]):
        if abs(prev["end"] - nxt["start"]) > 0.05:
            problems.append(f"gap between {prev['kind']} and {nxt['kind']}")
    for s in segs:
        if s["duration"] < MIN_SEG_SECONDS:
            problems.append(f"{s['kind']} shorter than {MIN_SEG_SECONDS}s")
        if abs((s["end"] - s["start"]) - s["duration"]) > 0.05:
            problems.append(f"{s['kind']} start/end/duration inconsistent")
        if not s["text"].strip():
            problems.append(f"{s['kind']} has empty text")

    total = script["est_total_seconds"]
    lo_s, hi_s = RUNTIME_BOUNDS
    if not (lo_s <= total <= hi_s):
        problems.append(f"est runtime {total}s outside {RUNTIME_BOUNDS}")

    if len(script["xpost"]) > 280:
        problems.append("xpost over 280 chars")
    if "#okxai" not in script["xpost"]:
        problems.append("xpost missing #okxai")

    return problems


def render_script_txt(script: dict, brief: dict) -> str:
    """Human-readable script file with the timing map."""
    lines = [
        f"OKX MARKET BRIEF - {script['date']}  ({script['mode']} mode)",
        f"Story of the day: {script['story_of_the_day']}",
        f"Words: {script['word_count']}  |  Est. runtime: {script['est_total_seconds']}s "
        f"@ {script['wps']} wps  |  Sentiment: {brief['sentiment']['label']} "
        f"({brief['sentiment']['score']:+.2f})",
        "-" * 72,
    ]
    for s in script["segments"]:
        lines.append(f"[{s['start']:05.1f}s - {s['end']:05.1f}s] {s['kind'].upper()} "
                     f"({s['words']} words)")
        lines.append(f"  ON-SCREEN: {s['caption']}")
        lines.append(f"  VO: {s['text']}")
        lines.append("")
    lines += ["-" * 72, "FULL VO SCRIPT:", "", script["full_text"], "",
              "-" * 72, "X POST:", "", script["xpost"], ""]
    return "\n".join(lines)
