"""Market Brief Studio -- web studio + service API (Python stdlib only).

    python serve.py                  # -> http://localhost:4105
    PORT=5000  overrides the port; HOST=0.0.0.0 exposes it on the LAN.

Routes
    GET  /               studio front page: today's episode (embedded
                         storyboard player), archive grid, X-post copy card,
                         watchlist settings card
    GET  /briefs/<file>  artifacts served from out/ (storyboard .html, voice
                         .mp3, script/xpost .txt, data .json)
    GET  /api/health     {"ok": true, ...}
    GET  /api/briefs     JSON archive index
    GET  /api/watchlist  watchlist preferences (state/preferences.json)
    POST /api/watchlist  {"symbols": ["BTC", ...]} -> saved preferences
    POST /api/generate   tape a brief: runs the pipeline in-process (mock data
                         mode). Optional JSON body {"date": "YYYY-MM-DD",
                         "audio": true}. TTS is attempted but degrades
                         gracefully to the silent storyboard; the response's
                         "audio" field reports what actually happened. A lock
                         guards concurrent runs (HTTP 409 while taping).

x402 pay-per-call (X402_MODE=off|mock|real, default off -- see x402_gate.py):
    when on, POST /api/generate is the ONLY gated route (5 USDT per call).
    No payment header -> HTTP 402 + PAYMENT-REQUIRED challenge header
    (base64 of the full {x402Version, resource, accepts} object); a valid
    PAYMENT-SIGNATURE (v2, checked first) or legacy X-PAYMENT header ->
    verify+settle via adapters/facilitator.py, then the normal handler runs
    and the response carries a PAYMENT-RESPONSE receipt header.
    X402_MODE=off (the default) is a transparent passthrough.

No pip installs and no external CDNs -- system font stacks + inline SVG only,
so the studio renders fully offline. (The optional voice stage needs the local
TTS server + internet, exactly like `python pipeline.py`.)
"""

from __future__ import annotations

import datetime as _dt
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pipeline
import x402_gate

esc = html.escape

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"
STATE = HERE / "state"
PREFS_PATH = STATE / "preferences.json"

DEFAULT_PORT = 4105

BRIEF_NAME_RE = re.compile(r"^brief-(\d{4}-\d{2}-\d{2})\.html$")
SAFE_FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,10}$")
_STORY_RE = re.compile(r"^Story of the day:\s*(.+?)\s*$", re.M)
_META_RE = re.compile(r"^Words:\s*(\d+)\s*\|\s*Est\. runtime:\s*([0-9.]+)s", re.M)
_VOICED_META_RE = re.compile(r'name="mbs-voiced" content="(yes|no)"')

WATCHLIST_CHOICES = ["BTC", "ETH", "SOL", "XRP", "TON", "LINK",
                     "DOGE", "PEPE", "APT", "WLD", "ARB", "OP"]
DEFAULT_WATCHLIST = ["BTC", "ETH", "SOL"]

GEN_LOCK = threading.Lock()  # one taping at a time

_CTYPES = {
    ".html": "text/html; charset=utf-8",
    ".mp3": "audio/mpeg",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
}


# --------------------------------------------------------------------------
# Archive model (reads out/, written by the pipeline)
# --------------------------------------------------------------------------

def _read_text(path: Path, limit: int | None = None) -> str:
    try:
        if limit is None:
            return path.read_text(encoding="utf-8", errors="replace")
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def brief_voiced(date: str, out_dir: Path) -> bool:
    """True when the storyboard has the voiceover embedded.

    Storyboards carry <meta name="mbs-voiced" content="yes|no"> in their head;
    for storyboards generated before that marker existed, fall back to
    "the mp3 file exists"."""
    head = _read_text(out_dir / f"brief-{date}.html", limit=2048)
    m = _VOICED_META_RE.search(head)
    if m:
        return m.group(1) == "yes"
    return (out_dir / f"brief-{date}.mp3").exists()


def list_briefs(out_dir: Path = OUT) -> list[dict]:
    """Archive index, newest first. Headline comes from the script file's
    'Story of the day:' line (brief JSON as fallback)."""
    if not out_dir.is_dir():
        return []
    briefs = []
    for p in out_dir.glob("brief-*.html"):
        m = BRIEF_NAME_RE.match(p.name)
        if not m:
            continue
        date = m.group(1)
        script_txt = _read_text(out_dir / f"script-{date}.txt", limit=4000)
        story = _STORY_RE.search(script_txt)
        if story:
            headline = story.group(1)
        else:
            try:
                headline = json.loads(
                    _read_text(out_dir / f"brief-{date}.json"))["story_of_the_day"]
            except (ValueError, KeyError, TypeError):
                headline = "Daily market brief"
        meta = _META_RE.search(script_txt)
        xpost = _read_text(out_dir / f"xpost-{date}.txt").strip()
        briefs.append({
            "date": date,
            "headline": headline,
            "words": int(meta.group(1)) if meta else None,
            "runtime": float(meta.group(2)) if meta else None,
            "voiced": brief_voiced(date, out_dir),
            "storyboard": f"/briefs/{p.name}",
            "script": (f"/briefs/script-{date}.txt"
                       if (out_dir / f"script-{date}.txt").exists() else None),
            "xpost": xpost or None,
            "xpost_url": (f"/briefs/xpost-{date}.txt"
                          if (out_dir / f"xpost-{date}.txt").exists() else None),
            "mp3": (f"/briefs/brief-{date}.mp3"
                    if (out_dir / f"brief-{date}.mp3").exists() else None),
        })
    briefs.sort(key=lambda b: b["date"], reverse=True)
    return briefs


def resolve_brief_file(name: str, out_dir: Path = OUT) -> Path | None:
    """Map /briefs/<name> to a file inside out/ -- or None (guards traversal)."""
    if not SAFE_FILE_RE.match(name):
        return None
    try:
        p = (out_dir / name).resolve()
    except OSError:
        return None
    if p.parent != out_dir.resolve() or not p.is_file():
        return None
    return p


# --------------------------------------------------------------------------
# Watchlist preferences (state/preferences.json)
# --------------------------------------------------------------------------

def _clean_symbols(symbols) -> list[str]:
    out, seen = [], set()
    for s in symbols or []:
        s = str(s).strip().upper()
        if SYMBOL_RE.match(s) and s not in seen:
            seen.add(s)
            out.append(s)
    return out[:24]


def load_prefs(path: Path = PREFS_PATH) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        syms = _clean_symbols(data.get("symbols"))
        if syms:
            return {"symbols": syms, "updated_utc": data.get("updated_utc")}
    except (OSError, ValueError, AttributeError):
        pass
    return {"symbols": list(DEFAULT_WATCHLIST), "updated_utc": None}


def save_prefs(symbols, path: Path = PREFS_PATH) -> dict:
    prefs = {
        "symbols": _clean_symbols(symbols),
        "updated_utc": _dt.datetime.now(_dt.timezone.utc)
                                   .isoformat(timespec="seconds"),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    return prefs


# --------------------------------------------------------------------------
# Generation (the service endpoint's engine)
# --------------------------------------------------------------------------

def run_generation(date: str | None, want_audio: bool) -> dict:
    """One guarded in-process pipeline run. Returns a JSON-able dict with
    "_status" carrying the HTTP code."""
    if not GEN_LOCK.acquire(blocking=False):
        return {"_status": 409, "ok": False,
                "error": "a brief is already being taped; try again shortly"}
    t0 = time.time()
    try:
        result = pipeline.generate(
            mode="mock", date=date, no_audio=not want_audio,
            log=lambda st, m: print(f"[gen:{st:>7}] {m}", flush=True))
    except pipeline.PipelineError as e:
        return {"_status": 500, "ok": False, "stage": e.stage, "error": str(e)}
    except Exception as e:  # report, never crash the studio
        return {"_status": 500, "ok": False,
                "error": f"{type(e).__name__}: {e}"}
    finally:
        GEN_LOCK.release()
    return {
        "_status": 200,
        "ok": True,
        "date": result["date"],
        "mode": result["mode"],
        "story": result["story"],
        "audio": result["audio"],
        "audio_info": result["audio_info"],
        "artifacts": {k: (f"/briefs/{v}" if v else None)
                      for k, v in result["artifacts"].items()},
        "elapsed_s": round(time.time() - t0, 1),
    }


# --------------------------------------------------------------------------
# Front page (all inline: system fonts + inline SVG; zero external assets)
# --------------------------------------------------------------------------

_FAVICON = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='14' fill='%23ff3d1f'/%3E"
    "%3Cpath d='M8 13h40' stroke='rgba(0,0,0,.35)' stroke-width='4' "
    "stroke-dasharray='9 7' stroke-linecap='round'/%3E"
    "%3Crect x='25' y='19' width='14' height='22' rx='7' fill='%23fff'/%3E"
    "%3Cpath d='M18 37a14 14 0 0 0 28 0' fill='none' stroke='%23fff' "
    "stroke-width='5' stroke-linecap='round'/%3E"
    "%3Cpath d='M32 51v6' stroke='%23fff' stroke-width='5' "
    "stroke-linecap='round'/%3E"
    "%3Ccircle cx='53' cy='12' r='5' fill='%23fff'/%3E"
    "%3C/svg%3E"
)

_MARK_SVG = """<svg class="mark" viewBox="0 0 48 48" width="40" height="40" aria-hidden="true">
  <defs><linearGradient id="mg" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#ff8a2a"/><stop offset="1" stop-color="#ff3d1f"/>
  </linearGradient></defs>
  <rect width="48" height="48" rx="11" fill="url(#mg)"/>
  <path d="M6 10h36" stroke="rgba(10,5,3,.4)" stroke-width="3" stroke-dasharray="8 6" stroke-linecap="round"/>
  <rect x="19" y="15" width="10" height="17" rx="5" fill="#fff"/>
  <path d="M14 28a10 10 0 0 0 20 0" fill="none" stroke="#fff" stroke-width="3.4" stroke-linecap="round"/>
  <path d="M24 38v5" stroke="#fff" stroke-width="3.4" stroke-linecap="round"/>
  <circle cx="40" cy="9" r="3.4" fill="#fff"/>
</svg>"""

_ONAIR_ON = ('<span class="onair disp"><i class="reddot"></i>On air</span>')
_ONAIR_OFF = ('<span class="onair off disp"><i class="reddot off"></i>'
              'Off air</span>')

_HERO_TODAY = """
<section class="hero">
  <div>
    <div class="kicker disp"><i class="reddot"></i>Today's episode &middot; __H_DATE__</div>
    <h1 class="disp">__H_HEADLINE__</h1>
    <div class="meta-row">__H_TAGS__<span class="tag">Mock data</span></div>
    <div class="cta-row">
      <a class="btn ghost disp" href="__H_BOARD__" target="_blank">Open player full-size</a>
      <button class="btn disp" id="genbtn" onclick="tape()">Re-tape today's brief</button>
    </div>
    <div id="genstatus" class="genstatus"></div>
  </div>
  <div class="monitor">
    <div class="monitor-top disp"><span><i class="reddot"></i>Rec</span><span>PGM 1 &middot; __H_DATE__</span></div>
    <iframe src="__H_BOARD__" title="Today's brief storyboard player" loading="lazy"></iframe>
  </div>
</section>"""

_HERO_EMPTY = """
<section class="hero">
  <div>
    <div class="kicker off disp"><i class="reddot off"></i>Off air &middot; __H_DATE__</div>
    <h1 class="disp">No brief taped yet today</h1>
    <p class="lede">One click runs the whole pipeline in-process: mock market data
      &rarr; 150-word broadcast script &rarr; voiceover (when the TTS rig answers)
      &rarr; storyboard player. Seconds when silent; up to a minute with voice.</p>
    <div class="cta-row">
      <button class="btn big disp" id="genbtn" onclick="tape()">Tape today's brief</button>
    </div>
    <div id="genstatus" class="genstatus"></div>
  </div>
  <div class="monitor">
    <div class="monitor-top disp"><span><i class="reddot off"></i>Stby</span><span>PGM 1 &middot; __H_DATE__</span></div>
    <div class="static disp">Stand by<span>No signal &middot; tape the first episode</span></div>
  </div>
</section>"""

_EMPTY_ARCHIVE = ('<p class="hint">Nothing in the archive yet - '
                  'tape your first brief above.</p>')

_PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="dark">
<title>Market Brief Studio - On Air</title>
<link rel="icon" href="__FAVICON__">
<style>
*{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0b0705;--bg2:#150d08;--panel:#181009;--panel2:#20150c;
  --edge:#2f2114;--edge2:#4b3520;
  --ink:#f8f1e7;--dim:#a8937f;--faint:#7a6650;
  --hot:#ff3d1f;--hot2:#ff8a2a;--glow:rgba(255,61,31,.32);--ok:#5cd47f;
}
html{color-scheme:dark}
body{
  min-height:100vh;color:var(--ink);
  font:15px/1.55 "Segoe UI",system-ui,-apple-system,Roboto,"Helvetica Neue",Arial,sans-serif;
  background:
    radial-gradient(1200px 520px at 78% -12%,rgba(255,61,31,.16),transparent 60%),
    radial-gradient(900px 520px at -12% 4%,rgba(255,138,42,.09),transparent 55%),
    linear-gradient(180deg,var(--bg2),var(--bg) 520px);
  background-color:var(--bg);
}
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:0;
  background:repeating-linear-gradient(0deg,rgba(255,255,255,.015) 0 1px,transparent 1px 3px)}
.disp{font-family:"Bahnschrift Condensed","Bahnschrift SemiCondensed",Bahnschrift,
  "Arial Narrow","Roboto Condensed","Liberation Sans Narrow","DejaVu Sans Condensed",
  Impact,Arial,sans-serif;font-stretch:condensed;text-transform:uppercase}
#page{position:relative;z-index:1;max-width:1180px;margin:0 auto;padding:26px 26px 56px}
a{color:inherit}
::selection{background:rgba(255,61,31,.45)}
.top{display:flex;align-items:center;justify-content:space-between;gap:14px;
  flex-wrap:wrap;padding-bottom:20px;border-bottom:1px solid var(--edge)}
.brand{display:flex;align-items:center;gap:13px;text-decoration:none}
.wm{display:flex;flex-direction:column;line-height:1}
.wm .l1{font-size:20px;font-weight:700;letter-spacing:.18em}
.wm .l2{margin-top:4px;font-size:11px;font-weight:700;letter-spacing:.58em;color:var(--hot)}
.topright{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.badge{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--edge2);
  border-radius:999px;padding:5px 12px;font-size:11px;font-weight:700;letter-spacing:.14em;
  color:var(--dim);text-transform:uppercase;background:rgba(0,0,0,.25)}
.badge.demo{color:var(--hot2);border-color:rgba(255,138,42,.45);background:rgba(255,138,42,.07)}
.badge.soon{color:var(--hot2);border-color:rgba(255,138,42,.4);background:rgba(255,138,42,.06);font-size:10px}
.onair{display:inline-flex;align-items:center;gap:9px;padding:8px 16px;border-radius:8px;
  font-size:14px;font-weight:700;letter-spacing:.3em;color:#ff5a3c;
  border:1px solid rgba(255,61,31,.6);background:rgba(255,61,31,.09);
  box-shadow:0 0 26px var(--glow),inset 0 0 14px rgba(255,61,31,.14);
  text-shadow:0 0 12px rgba(255,61,31,.7)}
.onair.off{color:var(--faint);border-color:var(--edge2);background:transparent;
  box-shadow:none;text-shadow:none}
.reddot{width:9px;height:9px;border-radius:50%;background:var(--hot);
  box-shadow:0 0 10px var(--hot);display:inline-block;flex:none;
  animation:pulse 1.15s ease-in-out infinite}
.reddot.off{background:var(--faint);box-shadow:none;animation:none}
.dot-s{width:6px;height:6px}
@keyframes pulse{50%{opacity:.25}}
.hero{display:grid;grid-template-columns:minmax(300px,5fr) minmax(320px,6fr);
  gap:36px;align-items:center;padding:42px 0 46px;border-bottom:1px solid var(--edge)}
.kicker{display:inline-flex;align-items:center;gap:10px;font-size:13px;font-weight:700;
  letter-spacing:.3em;color:var(--hot)}
.kicker.off{color:var(--faint)}
.hero h1{font-size:clamp(36px,5vw,62px);font-weight:700;line-height:.98;
  letter-spacing:.005em;margin:16px 0 18px;text-wrap:balance}
.lede{color:var(--dim);max-width:58ch;margin-bottom:20px}
.meta-row{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:24px}
.tag{display:inline-flex;align-items:center;gap:6px;border:1px solid var(--edge2);
  border-radius:6px;padding:4px 10px;font-size:11px;letter-spacing:.12em;color:var(--dim);
  text-transform:uppercase;font-weight:700}
.tag.voiced{color:#ff7a5e;border-color:rgba(255,61,31,.5)}
.cta-row{display:flex;gap:12px;flex-wrap:wrap;align-items:center}
.btn{display:inline-flex;align-items:center;gap:10px;border:0;border-radius:9px;
  padding:13px 24px;font-size:14px;font-weight:700;letter-spacing:.14em;cursor:pointer;
  color:#fff;background:linear-gradient(180deg,#ff5a2b,#e42f10);
  box-shadow:0 8px 28px var(--glow);text-decoration:none;text-transform:uppercase}
.btn:hover{filter:brightness(1.09)}
.btn[disabled]{opacity:.55;cursor:wait}
.btn.ghost{background:transparent;border:1px solid var(--edge2);color:var(--ink);box-shadow:none}
.btn.ghost:hover{border-color:var(--hot2)}
.btn.big{padding:16px 30px;font-size:16px}
.genstatus{margin-top:16px;font-size:13px;color:var(--dim);display:flex;gap:9px;
  align-items:center;min-height:20px}
.genstatus.err{color:#ff8570}
.genstatus.ok{color:var(--ok)}
.monitor{border:1px solid var(--edge2);border-radius:14px;overflow:hidden;background:#000;
  box-shadow:0 26px 70px rgba(0,0,0,.6),0 0 0 6px #17100a,0 0 60px rgba(255,61,31,.1)}
.monitor-top{display:flex;justify-content:space-between;align-items:center;padding:8px 14px;
  font-size:11px;letter-spacing:.22em;color:var(--dim);
  background:linear-gradient(180deg,#1c130b,#130c07);border-bottom:1px solid var(--edge)}
.monitor-top span{display:inline-flex;gap:8px;align-items:center}
.monitor iframe{display:block;width:100%;aspect-ratio:16/9.6;border:0;background:#05070c}
.static{aspect-ratio:16/9.6;display:flex;flex-direction:column;align-items:center;
  justify-content:center;gap:10px;color:var(--faint);font-size:26px;letter-spacing:.42em;
  background:repeating-linear-gradient(0deg,#0a0705 0 2px,#0e0906 2px 4px)}
.static span{font-size:11px;letter-spacing:.3em;color:#5b4b3a}
.rulehead{display:flex;align-items:center;gap:16px;margin:42px 0 20px}
.rulehead h2{font-size:24px;letter-spacing:.14em;font-weight:700;display:flex;
  align-items:center;gap:11px}
.rulehead h2::before{content:"";width:10px;height:10px;background:var(--hot);
  border-radius:50%;box-shadow:0 0 10px var(--hot)}
.rule{flex:1;height:1px;background:linear-gradient(90deg,var(--edge2),transparent)}
.count{font-size:12px;color:var(--faint);letter-spacing:.24em}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(252px,1fr));gap:18px}
.card{position:relative;border:1px solid var(--edge);border-radius:14px;
  background:linear-gradient(180deg,var(--panel2),var(--panel));padding:20px 18px 14px;
  display:flex;flex-direction:column;gap:11px;
  transition:transform .18s ease,border-color .18s ease,box-shadow .18s ease}
.card:hover{transform:translateY(-3px);border-color:rgba(255,61,31,.55);
  box-shadow:0 14px 34px rgba(0,0,0,.45),0 0 24px rgba(255,61,31,.12)}
.card::before{content:"";position:absolute;top:0;left:18px;right:18px;height:3px;
  border-radius:0 0 4px 4px;background:linear-gradient(90deg,var(--hot),var(--hot2))}
.card-date{display:flex;align-items:baseline;gap:10px}
.card-date .dd{font-size:44px;font-weight:700;line-height:.9}
.card-date .mmyy{font-size:13px;letter-spacing:.24em;color:var(--dim);font-weight:700}
.latest{margin-left:auto;display:inline-flex;align-items:center;gap:6px;font-size:10px;
  letter-spacing:.22em;color:var(--hot);font-weight:700}
.card h3{font-size:19px;line-height:1.1;font-weight:700;letter-spacing:.01em;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.tags{display:flex;gap:6px;flex-wrap:wrap}
.card-links{display:flex;gap:14px;flex-wrap:wrap;margin-top:auto;padding-top:11px;
  border-top:1px dashed var(--edge2);font-size:12px;letter-spacing:.16em;font-weight:700}
.card-links a{color:var(--hot2);text-decoration:none}
.card-links a:hover{color:var(--hot);text-decoration:underline}
.duo{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:18px;margin-top:42px}
.panel{border:1px solid var(--edge);border-radius:14px;
  background:linear-gradient(180deg,var(--panel2),var(--panel));padding:20px}
.panel-head{display:flex;align-items:center;justify-content:space-between;gap:10px;
  margin-bottom:14px;flex-wrap:wrap}
.panel-head h2{font-size:17px;letter-spacing:.18em;font-weight:700;display:flex;
  align-items:center;gap:9px}
.panel-head h2::before{content:"";width:8px;height:8px;background:var(--hot);
  border-radius:50%;box-shadow:0 0 8px var(--hot)}
.panel pre{white-space:pre-wrap;word-break:break-word;background:rgba(0,0,0,.35);
  border:1px solid var(--edge);border-radius:10px;padding:14px;
  font:12.5px/1.65 Consolas,"Cascadia Mono",Menlo,monospace;color:#ecdfca}
.hint{margin-top:10px;font-size:12px;color:var(--faint)}
.hint code{color:var(--dim);font-family:Consolas,"Cascadia Mono",monospace}
.mini{border:1px solid rgba(255,61,31,.55);background:rgba(255,61,31,.1);color:#ff6a4d;
  border-radius:7px;padding:6px 14px;font-size:11px;font-weight:700;letter-spacing:.18em;
  cursor:pointer;text-transform:uppercase}
.mini:hover{background:rgba(255,61,31,.2)}
.chips{display:flex;gap:8px;flex-wrap:wrap}
.chip{position:relative}
.chip input{position:absolute;opacity:0;pointer-events:none}
.chip span{display:inline-block;border:1px solid var(--edge2);border-radius:8px;
  padding:7px 13px;font-size:12px;font-weight:700;letter-spacing:.1em;color:var(--dim);
  cursor:pointer;transition:.15s;user-select:none}
.chip span:hover{border-color:var(--hot2)}
.chip input:checked+span{color:#ffe3d9;border-color:var(--hot);
  background:rgba(255,61,31,.16);box-shadow:0 0 12px rgba(255,61,31,.15)}
.panel-foot{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}
footer{margin-top:52px;padding-top:18px;border-top:1px solid var(--edge);
  color:var(--faint);font-size:12px;letter-spacing:.08em;display:flex;
  justify-content:space-between;gap:10px;flex-wrap:wrap}
@media (max-width:860px){
  #page{padding:18px 14px 40px}
  .hero{grid-template-columns:1fr;gap:24px;padding:28px 0 34px}
  .hero h1{font-size:clamp(32px,9vw,48px)}
  .wm .l1{font-size:17px}
  .onair{font-size:12px;padding:6px 12px;letter-spacing:.22em}
  .card-date .dd{font-size:36px}
}
</style>
</head>
<body>
<div id="page">
  <header class="top">
    <a class="brand" href="/">
      __MARK__
      <span class="wm disp"><span class="l1">Market&nbsp;Brief</span><span class="l2">Studio</span></span>
    </a>
    <div class="topright">
      <span class="badge demo">Demo &middot; mock data</span>
      __ONAIR__
    </div>
  </header>

  __HERO__

  <section>
    <div class="rulehead">
      <h2 class="disp">The archive</h2>
      <span class="rule"></span>
      <span class="count disp">__COUNT__</span>
    </div>
    <div class="grid">__CARDS__</div>
  </section>

  <section class="duo">
    __XPOST_PANEL__
    <div class="panel">
      <div class="panel-head">
        <h2 class="disp">Your watchlist</h2>
        <span class="badge soon">Personalization coming with live data</span>
      </div>
      <div class="chips">__CHIPS__</div>
      <div class="panel-foot">
        <button class="mini disp" onclick="saveWatchlist(this)">Save watchlist</button>
        <span id="wlstatus" class="hint" style="margin-top:0"></span>
      </div>
      <div class="hint">Saved to <code>state/preferences.json</code>. Demo briefs cover the
        full mock tape; with live OKX data your picks drive the rundown.</div>
    </div>
  </section>

  <section class="panel" style="margin-top:18px">
    <div class="panel-head">
      <h2 class="disp">Service API</h2>
      <span class="badge demo">Mock mode</span>
    </div>
    <pre>GET  /api/health      -> {"ok": true, ...}
GET  /api/briefs      -> archive index (JSON)
POST /api/generate    -> tape a brief in-process; body optional:
                         {"date":"YYYY-MM-DD","audio":true}
                         response reports "audio": true|false; 409 while taping
GET  /briefs/&lt;file&gt;   -> storyboard .html / voice .mp3 / script .txt / .json</pre>
    <div class="hint">POST /api/generate is the future service endpoint: a daily scheduler
      or a subscriber's agent calls it, then fetches the artifact URLs it returns.</div>
  </section>

  <footer>
    <span>MARKET BRIEF STUDIO &middot; auto-generated daily voice+video market recap</span>
    <span>python stdlib only &middot; no external assets &middot; __TODAY__</span>
  </footer>
</div>
<script>
function _status(cls,msg){var el=document.getElementById('genstatus');
  if(!el)return;el.className='genstatus '+cls;
  el.innerHTML=(cls==='taping'?'<i class="reddot"></i>':'')+msg;}
async function tape(){
  var btn=document.getElementById('genbtn');if(!btn||btn.disabled)return;
  var label=btn.textContent;btn.disabled=true;btn.textContent='TAPING…';
  _status('taping','Rolling: data → script → voice → storyboard. '+
    'The voice render can take up to a minute; if the TTS rig is unreachable the brief ships silent.');
  try{
    var r=await fetch('/api/generate',{method:'POST',
      headers:{'Content-Type':'application/json'},body:'{}'});
    var j=await r.json().catch(function(){return {ok:false,error:'unreadable response'}});
    if(!j.ok){_status('err','Failed: '+(j.error||('HTTP '+r.status)));
      btn.disabled=false;btn.textContent=label;return;}
    _status('ok',(j.audio?'Taped WITH voiceover in '+j.elapsed_s+'s. Reloading…'
      :'Taped SILENT (no TTS audio — storyboard auto-advances). Reloading…'));
    setTimeout(function(){location.reload();},1600);
  }catch(e){_status('err','Network error: '+e);btn.disabled=false;btn.textContent=label;}
}
function copyText(t,btn){
  function done(ok){if(!btn)return;var old=btn.textContent;
    btn.textContent=ok?'COPIED':'COPY FAILED';
    setTimeout(function(){btn.textContent=old;},1400);}
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(t).then(function(){done(true)},function(){done(false)});
  }else{
    var ta=document.createElement('textarea');ta.value=t;document.body.appendChild(ta);
    ta.select();var ok=false;try{ok=document.execCommand('copy')}catch(e){}
    document.body.removeChild(ta);done(ok);
  }
}
function copyXpost(btn){var el=document.getElementById('xpost');
  if(el)copyText(el.textContent,btn);}
async function saveWatchlist(btn){
  var el=document.getElementById('wlstatus');
  var syms=Array.prototype.map.call(
    document.querySelectorAll('.chips input:checked'),function(i){return i.value});
  try{
    var r=await fetch('/api/watchlist',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({symbols:syms})});
    var j=await r.json();
    el.textContent=j.ok?('Saved: '+(j.symbols.length?j.symbols.join(', '):'(empty)')):
      ('Failed: '+(j.error||r.status));
  }catch(e){el.textContent='Network error';}
}
</script>
</body></html>
"""


def _date_parts(date: str) -> tuple[str, str, str]:
    try:
        d = _dt.date.fromisoformat(date)
    except ValueError:
        return date, "", ""
    return f"{d.day:02d}", d.strftime("%b").upper(), str(d.year)


def _voice_tag(voiced: bool) -> str:
    if voiced:
        return ('<span class="tag voiced"><i class="reddot dot-s"></i>'
                'Voiced</span>')
    return '<span class="tag">Silent</span>'


def _hero_html(today: str, b: dict | None) -> str:
    if b is None:
        return _HERO_EMPTY.replace("__H_DATE__", esc(today))
    tags = _voice_tag(b["voiced"])
    if b["words"]:
        tags += f'<span class="tag">{b["words"]} words</span>'
    if b["runtime"]:
        tags += f'<span class="tag">{b["runtime"]:.0f}s runtime</span>'
    return (_HERO_TODAY
            .replace("__H_DATE__", esc(b["date"]))
            .replace("__H_HEADLINE__", esc(b["headline"]))
            .replace("__H_TAGS__", tags)
            .replace("__H_BOARD__", esc(b["storyboard"])))


def _card_html(b: dict, latest: bool) -> str:
    dd, mon, yyyy = _date_parts(b["date"])
    tags = _voice_tag(b["voiced"])
    if b["words"]:
        tags += f'<span class="tag">{b["words"]} words</span>'
    if b["runtime"]:
        tags += f'<span class="tag">{b["runtime"]:.0f}s</span>'
    links = f'<a href="{esc(b["storyboard"])}" target="_blank">Watch</a>'
    if b["script"]:
        links += f'<a href="{esc(b["script"])}" target="_blank">Script</a>'
    if b["mp3"]:
        links += f'<a href="{esc(b["mp3"])}" target="_blank">MP3</a>'
    if b["xpost_url"]:
        links += f'<a href="{esc(b["xpost_url"])}" target="_blank">X post</a>'
    latest_html = ('<span class="latest"><i class="reddot dot-s"></i>Latest</span>'
                   if latest else "")
    return (f'<article class="card">'
            f'<div class="card-date disp"><span class="dd">{esc(dd)}</span>'
            f'<span class="mmyy">{esc(mon)} {esc(yyyy)}</span>{latest_html}</div>'
            f'<h3 class="disp">{esc(b["headline"])}</h3>'
            f'<div class="tags">{tags}</div>'
            f'<div class="card-links disp">{links}</div>'
            f'</article>')


def _xpost_panel(b: dict | None) -> str:
    if b is None or not b.get("xpost"):
        return ('<div class="panel"><div class="panel-head">'
                '<h2 class="disp">X post</h2></div>'
                '<pre id="xpost">Tape your first brief to get the day\'s '
                'post text.</pre></div>')
    return (f'<div class="panel"><div class="panel-head">'
            f'<h2 class="disp">X post &middot; {esc(b["date"])}</h2>'
            f'<button class="mini disp" onclick="copyXpost(this)">Copy</button></div>'
            f'<pre id="xpost">{esc(b["xpost"])}</pre>'
            f'<div class="hint">{len(b["xpost"])}/280 chars &middot; ships with '
            f'#okxai &middot; also at <code>out/xpost-{esc(b["date"])}.txt</code></div>'
            f'</div>')


def _chips_html(selected: list[str]) -> str:
    choices = list(WATCHLIST_CHOICES)
    for s in selected:
        if s not in choices:
            choices.append(s)
    return "".join(
        f'<label class="chip"><input type="checkbox" value="{esc(s)}"'
        f'{" checked" if s in selected else ""}><span>{esc(s)}</span></label>'
        for s in choices)


def render_home(out_dir: Path = OUT) -> str:
    briefs = list_briefs(out_dir)
    today = _dt.date.today().isoformat()
    today_b = next((b for b in briefs if b["date"] == today), None)
    xpost_b = today_b or (briefs[0] if briefs else None)
    prefs = load_prefs()
    count = f"{len(briefs)} episode" + ("" if len(briefs) == 1 else "s")
    cards = "".join(_card_html(b, i == 0) for i, b in enumerate(briefs))
    return (_PAGE
            .replace("__FAVICON__", _FAVICON)
            .replace("__MARK__", _MARK_SVG)
            .replace("__ONAIR__", _ONAIR_ON if today_b else _ONAIR_OFF)
            .replace("__HERO__", _hero_html(today, today_b))
            .replace("__COUNT__", esc(count))
            .replace("__CARDS__", cards or _EMPTY_ARCHIVE)
            .replace("__XPOST_PANEL__", _xpost_panel(xpost_b))
            .replace("__CHIPS__", _chips_html(prefs["symbols"]))
            .replace("__TODAY__", esc(today)))


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class StudioHandler(BaseHTTPRequestHandler):
    server_version = "MarketBriefStudio/0.1"

    # ---- plumbing ----
    def _send(self, code: int, body: bytes, ctype: str,
              extra_headers: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, obj: dict, code: int = 200,
                  extra_headers: dict | None = None) -> None:
        self._send(code, json.dumps(obj).encode("utf-8"),
                   "application/json; charset=utf-8", extra_headers)

    def send_html(self, text: str, code: int = 200) -> None:
        self._send(code, text.encode("utf-8"), "text/html; charset=utf-8")

    def _body_json(self) -> dict | None:
        """Parsed JSON body; {} when empty; None when unparseable/too big."""
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        if n < 0 or n > 1_000_000:
            return None
        raw = self.rfile.read(n) if n else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def log_message(self, fmt, *args):  # concise one-line log
        sys.stdout.write("[http] %s - %s\n" % (self.address_string(),
                                               fmt % args))
        sys.stdout.flush()

    # ---- GET ----
    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            return self.send_html(render_home())
        if path == "/api/health":
            return self.send_json({
                "ok": True,
                "service": "market-brief-studio",
                "mode": "mock",
                "generating": GEN_LOCK.locked(),
                "briefs": len(list_briefs()),
                "today": _dt.date.today().isoformat(),
            })
        if path == "/api/briefs":
            return self.send_json({"ok": True, "briefs": list_briefs()})
        if path == "/api/watchlist":
            return self.send_json({"ok": True,
                                   "available": WATCHLIST_CHOICES,
                                   **load_prefs()})
        if path in ("/favicon.ico", "/favicon.svg"):
            svg = urllib.parse.unquote(_FAVICON.split(",", 1)[1])
            return self._send(200, svg.encode("utf-8"), "image/svg+xml")
        if path.startswith("/briefs/"):
            p = resolve_brief_file(path[len("/briefs/"):])
            if p is None:
                return self.send_json({"ok": False,
                                       "error": "no such artifact"}, 404)
            ctype = _CTYPES.get(p.suffix.lower(), "application/octet-stream")
            return self._send(200, p.read_bytes(), ctype)
        return self.send_json({"ok": False, "error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        body = self._body_json()
        if body is None:
            return self.send_json(
                {"ok": False, "error": "body must be JSON (or empty)"}, 400)

        if path == "/api/generate":
            # x402 pay-per-call gate (X402_MODE=off -> transparent no-op).
            # This is the ONLY gated route; everything else stays free.
            try:
                # PAYMENT-SIGNATURE (v2) first, legacy X-PAYMENT fallback.
                pay = x402_gate.check(self.headers)
            except ValueError as e:      # bad X402_MODE (server misconfig)
                return self.send_json({"ok": False, "error": str(e)}, 500)
            if not pay.allowed:
                return self.send_json(pay.body, pay.status, pay.headers)

            # pay.headers carries PAYMENT-RESPONSE once settled (empty when
            # X402_MODE=off), so a paid caller always gets their receipt.
            if body.get("mode", "mock") != "mock":
                return self.send_json(
                    {"ok": False, "error": "this demo server generates in mock "
                     "mode only (live data is on the roadmap)"}, 400,
                    pay.headers)
            date = body.get("date")
            if date is not None:
                try:
                    date = _dt.date.fromisoformat(str(date)).isoformat()
                except ValueError:
                    return self.send_json(
                        {"ok": False, "error": "date must be YYYY-MM-DD"},
                        400, pay.headers)
            want_audio = body.get("audio", True)
            if not isinstance(want_audio, bool):
                return self.send_json(
                    {"ok": False, "error": "audio must be true/false"},
                    400, pay.headers)
            result = run_generation(date, want_audio)
            return self.send_json(result, result.pop("_status"), pay.headers)

        if path == "/api/watchlist":
            syms = body.get("symbols")
            if not isinstance(syms, list):
                return self.send_json(
                    {"ok": False,
                     "error": 'expected {"symbols": ["BTC", ...]}'}, 400)
            return self.send_json({"ok": True, **save_prefs(syms)})

        return self.send_json({"ok": False, "error": "not found"}, 404)


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> int:
    port = int(os.environ.get("PORT", DEFAULT_PORT))
    # Bind all interfaces by default so containerized/PaaS deploys (Railway,
    # Render, etc.) are reachable; override with HOST for a local-only bind.
    host = os.environ.get("HOST", "0.0.0.0")
    OUT.mkdir(exist_ok=True)
    STATE.mkdir(exist_ok=True)
    try:
        httpd = ThreadingHTTPServer((host, port), StudioHandler)
    except OSError as e:
        print(f"[serve] cannot bind {host}:{port} - {e}", flush=True)
        print("[serve] set PORT=<other port> and retry", flush=True)
        return 1
    shown = "localhost" if host in ("0.0.0.0", "127.0.0.1") else host
    print("=" * 64, flush=True)
    print("  MARKET BRIEF STUDIO - ON AIR (demo / mock data)", flush=True)
    print(f"  studio front page : http://{shown}:{port}/", flush=True)
    print("  service api       : POST /api/generate   GET /api/health", flush=True)
    print(f"  artifacts         : {OUT}", flush=True)
    print("  Ctrl+C stops the studio.", flush=True)
    print("=" * 64, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] off air.", flush=True)
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
