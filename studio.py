"""Studio: voice (TTS) + visuals (storyboard HTML, video stub) for the brief.

TTS  -- wired to the existing server at C:/Users/USER/tts_server.py:
        POST http://localhost:8765  {"text": ..., "video_id": ...}
        -> shells out to `edge-tts --voice en-US-GuyNeural` (Microsoft online
        voices, needs internet) and writes C:/Users/USER/audio_<video_id>.mp3,
        replying {"success": true, "audio_path": ...}.
        The server has no health route (any POST synthesizes), so liveness is
        a TCP connect. If it isn't running, we start it ourselves and stop it
        again afterwards. Output is COPIED into out/brief-<date>.mp3; the
        server's own copy stays where it always writes it (its design).
        Degrades gracefully: missing edge-tts / no internet / server failure
        -> (None, reason) and the pipeline continues without audio.

VIDEO -- the existing C:/Users/USER/video_server.py (port 8766) renders
        stickman narrative scenes (PIL) and concats them with ffmpeg. Wrong
        visual language for a market brief and its build step only consumes
        frames its own draw_scenes wrote under C:/Users/USER/videos/<id>/
        (outside this project's write sandbox), so mp4 assembly stays a
        documented stub -- see VIDEO_INTEGRATION_STEPS / assemble_video().
        The guaranteed visual deliverable is the self-contained HTML
        storyboard (out/brief-<date>.html): dark theme, big numbers, one
        auto-advancing slide per script segment, synced to the timing map,
        with the voiceover embedded (base64) when TTS succeeded.
"""

from __future__ import annotations

import base64
import html
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from adapters import llm, okx_data

esc = html.escape

# --------------------------------------------------------------------------
# Existing servers on this machine (READ-ONLY - never modified)
# --------------------------------------------------------------------------
TTS_HOST, TTS_PORT = "localhost", 8765
TTS_URL = f"http://{TTS_HOST}:{TTS_PORT}"
TTS_SERVER_SCRIPT = r"C:\Users\USER\tts_server.py"
TTS_OUTPUT_PATTERN = "C:/Users/USER/audio_{video_id}.mp3"  # server hardcodes this

VIDEO_HOST, VIDEO_PORT = "localhost", 8766
VIDEO_URL = f"http://{VIDEO_HOST}:{VIDEO_PORT}"
VIDEO_SERVER_SCRIPT = r"C:\Users\USER\video_server.py"

VIDEO_INTEGRATION_STEPS = """\
video_server.py API (inspected, not wired for this product):
  POST {url}  {{"action": "draw_scenes", "video_id": ID,
                "scenes": [{{"setting", "characters", "image_prompt",
                             "text_card", "camera"}}...],
                "frames_per_scene": [n, ...]}}
       -> PIL stickman frames in C:/Users/USER/videos/<ID>/frames + frames_list.json
  POST {url}  {{"action": "build_video", "video_id": ID, "title": T,
                "sound_cues": [...], "scenes": [...]}}
       -> ffmpeg concat @8fps + sound cues from C:/Users/USER/sounds/
       -> C:/Users/USER/videos/<ID>_<title>.mp4

Why it is stubbed for the market brief:
  1. Its renderer draws stickman narrative scenes (battlefield/castle/...),
     not caption/number frames - wrong visual language for a market recap.
  2. build_video only consumes frames that draw_scenes wrote under
     C:/Users/USER/videos/<ID>/; injecting our own frames there is outside
     this project's allowed write path (market-brief-studio only).

Direct mp4 path (ffmpeg IS installed on this box), when mp4 is wanted:
  1. Render each slide of out/frames-<date>.json to PNG at 1280x720
     (Pillow is installed) into out/frames/.
  2. ffmpeg -y -f concat -safe 0 -i out/frames/filelist.txt
       -vf scale=1280:720 -c:v libx264 -pix_fmt yuv420p -r 30
       out/brief-<date>-silent.mp4
     (filelist.txt: `file 'NNN.png'` + `duration <seg duration>` per slide,
      matching the timing map exactly.)
  3. ffmpeg -y -i out/brief-<date>-silent.mp4 -i out/brief-<date>.mp3
       -c:v copy -c:a aac -shortest out/brief-<date>.mp4
""".format(url=VIDEO_URL)


# --------------------------------------------------------------------------
# small utilities
# --------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _post_json(url: str, payload: dict, timeout: float = 120.0) -> dict:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# --------------------------------------------------------------------------
# TTS stage
# --------------------------------------------------------------------------

def tts_preflight() -> str | None:
    """Return a blocking reason, or None if TTS looks feasible."""
    if not Path(TTS_SERVER_SCRIPT).exists():
        return f"TTS server script not found at {TTS_SERVER_SCRIPT}"
    if shutil.which("edge-tts") is None:
        return ("edge-tts CLI not on PATH (pip install edge-tts); the TTS "
                "server shells out to it")
    return None


def _start_tts_server() -> subprocess.Popen | None:
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            [sys.executable, TTS_SERVER_SCRIPT],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
    except OSError:
        return None
    for _ in range(40):  # up to ~8s for the port to come up
        if _port_open(TTS_HOST, TTS_PORT):
            return proc
        if proc.poll() is not None:
            return None
        time.sleep(0.2)
    proc.terminate()
    return None


def synthesize(text: str, date: str, out_dir: Path) -> tuple[Path | None, str]:
    """Render `text` to out_dir/brief-<date>.mp3 via the existing TTS server.

    Returns (path, info) on success or (None, reason) on graceful failure.
    """
    reason = tts_preflight()
    if reason:
        return None, reason

    started = None
    if not _port_open(TTS_HOST, TTS_PORT):
        started = _start_tts_server()
        if started is None:
            return None, f"could not start {TTS_SERVER_SCRIPT} on :{TTS_PORT}"
        info_boot = "started tts_server.py for this run"
    else:
        info_boot = "tts_server.py already running"

    video_id = f"brief_{date.replace('-', '')}"
    attempts = 2  # edge-tts hits Microsoft's online voices; can be flaky
    try:
        last_err = "unknown"
        for attempt in range(1, attempts + 1):
            try:
                result = _post_json(TTS_URL, {"text": text, "video_id": video_id},
                                    timeout=90)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                last_err = f"TTS call failed (attempt {attempt}/{attempts}): {e}"
                continue
            if not result.get("success"):
                err = str(result.get("error", ""))[:300] or "unknown server error"
                last_err = (f"TTS server returned failure ({err}) - edge-tts "
                            "needs internet access to Microsoft voices")
                continue
            src = Path(result["audio_path"])
            if not src.exists() or src.stat().st_size == 0:
                last_err = f"TTS reported {src} but the file is missing/empty"
                continue
            dst = out_dir / f"brief-{date}.mp3"
            shutil.copyfile(src, dst)
            return dst, (f"{info_boot}; voice en-US-GuyNeural; "
                         f"{dst.stat().st_size/1024:.0f} KB "
                         f"(server's own copy remains at {src})")
        return None, last_err
    finally:
        if started is not None:
            started.terminate()


# --------------------------------------------------------------------------
# Video stage (stub, documented)
# --------------------------------------------------------------------------

def probe_video_server() -> str:
    if _port_open(VIDEO_HOST, VIDEO_PORT):
        return f"video_server.py IS listening on :{VIDEO_PORT}"
    return f"video_server.py not running (start: python {VIDEO_SERVER_SCRIPT})"


def assemble_video(script: dict, brief: dict, audio_path: Path | None) -> tuple[None, str]:
    """STUB - mp4 assembly is not wired (see module docstring).

    Returns (None, documented-integration-steps).
    """
    return None, f"{probe_video_server()}\n{VIDEO_INTEGRATION_STEPS}"


# --------------------------------------------------------------------------
# Frames data (caption frames for any video assembler)
# --------------------------------------------------------------------------

def build_frames_json(script: dict, brief: dict) -> dict:
    """Caption-frame data matching the timing map - the hand-off format for
    actual mp4 assembly (see VIDEO_INTEGRATION_STEPS)."""
    return {
        "date": script["date"],
        "resolution": [1280, 720],
        "wps": script["wps"],
        "total_seconds": script["est_total_seconds"],
        "audio": f"brief-{script['date']}.mp3",
        "frames": [
            {
                "idx": s["idx"], "kind": s["kind"],
                "start": s["start"], "end": s["end"], "duration": s["duration"],
                "caption": s["caption"], "spoken": s["text"], "stat": s["stat"],
            }
            for s in script["segments"]
        ],
    }


# --------------------------------------------------------------------------
# Storyboard HTML (the guaranteed visual deliverable)
# --------------------------------------------------------------------------

def _sparkline(date: str, key: str, direction: str, w: int = 240, h: int = 56) -> str:
    """Deterministic little price path ending in the move's direction."""
    rng = okx_data._rng_for(date, f"spark-{key}")
    n = 28
    drift = 0.55 if direction == "up" else -0.55
    ys, y = [], 0.0
    for _ in range(n):
        y += rng.uniform(-1, 1) + drift / n * 6
        ys.append(y)
    lo, hi = min(ys), max(ys)
    span = (hi - lo) or 1.0
    pts = []
    for i, v in enumerate(ys):
        px = round(i * (w / (n - 1)), 1)
        py = round(h - 6 - (v - lo) / span * (h - 12), 1)
        pts.append(f"{px},{py}")
    color = "var(--up)" if direction == "up" else "var(--down)"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
            f'<polyline fill="none" stroke="{color}" stroke-width="2.5" '
            f'stroke-linejoin="round" stroke-linecap="round" points="{" ".join(pts)}"/>'
            f'<circle cx="{pts[-1].split(",")[0]}" cy="{pts[-1].split(",")[1]}" r="3.5" fill="{color}"/>'
            f"</svg>")


def _row(label: str, value: str, delta: str | None, direction: str) -> str:
    cls = "up" if direction == "up" else "down"
    d = f'<span class="rdelta {cls}">{esc(delta)}</span>' if delta else ""
    return (f'<div class="row"><span class="rlabel">{esc(label)}</span>'
            f'<span class="rvalue">{esc(value)}</span>{d}</div>')


def _slide_rows(seg: dict, brief: dict) -> str:
    """Secondary number rows per slide kind."""
    btc = brief["majors"]["BTC-USDT"]
    eth = brief["majors"]["ETH-USDT"]
    g = brief["gainers"]
    l = brief["losers"]
    fund = {f["inst"]: f for f in brief["funding"]}

    def mrow(r):
        return _row(llm.base_sym(r["inst"]), llm.fmt_price(r["last"]),
                    llm.fmt_delta(r["chg24h_pct"]),
                    "up" if r["chg24h_pct"] >= 0 else "down")

    kind = seg["kind"]
    if kind == "hook":
        return (_row("BTC", llm.fmt_price(btc["last"]), llm.fmt_delta(btc["chg24h_pct"]),
                     "up" if btc["chg24h_pct"] >= 0 else "down")
                + _row("ETH", llm.fmt_price(eth["last"]), llm.fmt_delta(eth["chg24h_pct"]),
                       "up" if eth["chg24h_pct"] >= 0 else "down")
                + mrow(g[0]))
    if kind == "majors":
        return (_row("ETH-USDT", llm.fmt_price(eth["last"]), llm.fmt_delta(eth["chg24h_pct"]),
                     "up" if eth["chg24h_pct"] >= 0 else "down")
                + _row("24H RANGE", f"{llm.fmt_price(btc['low24h'])} - {llm.fmt_price(btc['high24h'])}", None, "up")
                + _row("BTC VOL", f"${btc['vol24h_usd']/1e9:.2f}B", None, "up"))
    if kind == "leader":
        lead_fund = fund.get(f"{llm.base_sym(g[0]['inst'])}-USDT-SWAP")
        rows = mrow(g[1]) + mrow(g[2])
        if lead_fund:
            rows += _row("FUNDING", f"{lead_fund['rate_pct']:+.3f}%", None,
                         "up" if lead_fund["rate_pct"] >= 0 else "down")
        return rows
    if kind == "laggards":
        lag_fund = fund.get(f"{llm.base_sym(l[0]['inst'])}-USDT-SWAP")
        rows = mrow(l[1]) + mrow(l[2])
        if lag_fund:
            rows += _row("FUNDING", f"{lag_fund['rate_pct']:+.3f}%", None,
                         "up" if lag_fund["rate_pct"] >= 0 else "down")
        return rows
    # outro: funding snapshot
    return "".join(_row(f["inst"].replace("-USDT-SWAP", " PERP"),
                        f"{f['rate_pct']:+.3f}%", None,
                        "up" if f["rate_pct"] >= 0 else "down")
                   for f in brief["funding"])


def _tape_items(brief: dict) -> str:
    bits = []
    rows = ([("BTC-USDT", brief["majors"]["BTC-USDT"]), ("ETH-USDT", brief["majors"]["ETH-USDT"])]
            + [(r["inst"], r) for r in brief["gainers"]]
            + [(r["inst"], r) for r in brief["losers"]])
    for inst, r in rows:
        chg = r["chg24h_pct"]
        cls = "up" if chg >= 0 else "down"
        bits.append(f'<span class="titem">{esc(inst)} <b>{esc(llm.fmt_price(r["last"]))}</b> '
                    f'<span class="{cls}">{esc(llm.fmt_delta(chg))}</span></span>')
    for f in brief["funding"]:
        cls = "up" if f["rate_pct"] >= 0 else "down"
        bits.append(f'<span class="titem">{esc(f["inst"])} funding '
                    f'<span class="{cls}">{f["rate_pct"]:+.3f}%</span></span>')
    return "".join(bits)


_KICKERS = {"hook": "TODAY'S TAPE", "majors": "STORY 1 - THE MAJORS",
            "leader": "STORY 2 - THE LEADER", "laggards": "STORY 3 - THE BLEED",
            "outro": "THAT'S THE BRIEF"}


def _render_slide(seg: dict, brief: dict) -> str:
    stat = seg.get("stat") or {}
    direction = stat.get("direction", "up")
    delta_cls = "up" if direction == "up" else "down"
    stat_html = ""
    if stat:
        stat_html = (
            f'<div class="stat"><div class="stat-label">{esc(stat.get("label", ""))}</div>'
            f'<div class="stat-line"><span class="stat-value">{esc(stat.get("value", ""))}</span>'
            f'<span class="stat-delta {delta_cls}">{esc(stat.get("delta", ""))}</span></div>'
            f'{_sparkline(brief["date"], f"{seg['idx']}-{stat.get('label', '')}", direction)}'
            f"</div>")
    return f"""
    <section class="slide" data-idx="{seg['idx']}">
      <div class="kicker">{esc(_KICKERS.get(seg['kind'], seg['kind'].upper()))}</div>
      {stat_html}
      <div class="rows">{_slide_rows(seg, brief)}</div>
      <p class="caption">{esc(seg['caption'])}</p>
      <p class="vo">{esc(seg['text'])}</p>
    </section>"""


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="mbs-voiced" content="__VOICED__">
<title>__TITLE__</title>
<style>
  :root{--bg:#05070c;--panel:#0b0f18;--edge:#1b2333;--ink:#eef2fa;--dim:#8b96ad;
        --accent:#6ea8ff;--up:#2bd47e;--down:#ff5c6b;}
  *{box-sizing:border-box;margin:0;padding:0}
  html,body{height:100%}
  body{background:radial-gradient(1200px 700px at 50% -10%,#0d1526 0%,var(--bg) 55%);
       color:var(--ink);font-family:"Segoe UI",system-ui,-apple-system,Arial,sans-serif;
       display:flex;align-items:center;justify-content:center;padding:24px}
  #wrap{width:min(1100px,96vw)}
  #stage{position:relative;aspect-ratio:16/9;background:
         linear-gradient(180deg,#0c1220 0%,#070a12 100%);
         border:1px solid var(--edge);border-radius:18px;overflow:hidden;
         box-shadow:0 30px 80px rgba(0,0,0,.55),0 0 0 1px rgba(110,168,255,.06),
                    0 0 120px rgba(110,168,255,.07);cursor:pointer}
  #stage::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.5;
         background:repeating-linear-gradient(0deg,transparent 0 79px,rgba(110,168,255,.045) 79px 80px),
                    repeating-linear-gradient(90deg,transparent 0 79px,rgba(110,168,255,.045) 79px 80px)}
  header{position:absolute;top:0;left:0;right:0;display:flex;align-items:center;
         justify-content:space-between;padding:22px 30px;z-index:3}
  .brand{display:flex;align-items:center;gap:12px;font-weight:800;letter-spacing:.14em;font-size:14px}
  .brand .sq{width:12px;height:12px;background:var(--ink);box-shadow:16px 0 0 var(--accent)}
  .brand span{margin-left:14px}
  .meta{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--dim);letter-spacing:.08em}
  .badge{border:1px solid var(--edge);border-radius:999px;padding:4px 10px;
         background:rgba(110,168,255,.08);color:var(--accent);font-weight:600}
  .live{color:var(--up)}.live::before{content:"";display:inline-block;width:7px;height:7px;
        border-radius:50%;background:var(--up);margin-right:6px;animation:blink 1.4s infinite}
  @keyframes blink{50%{opacity:.25}}
  .slide{position:absolute;inset:0;padding:86px 64px 96px;display:flex;flex-direction:column;
         justify-content:center;gap:14px;opacity:0;transform:translateY(14px) scale(.985);
         transition:opacity .5s ease,transform .5s ease;pointer-events:none;z-index:2}
  .slide.active{opacity:1;transform:none;pointer-events:auto}
  .kicker{color:var(--accent);font-size:13px;font-weight:800;letter-spacing:.3em}
  .stat-label{color:var(--dim);font-size:15px;font-weight:700;letter-spacing:.18em;margin-bottom:2px}
  .stat-line{display:flex;align-items:baseline;gap:20px;flex-wrap:wrap}
  .stat-value{font-size:clamp(46px,7.6vw,96px);font-weight:800;letter-spacing:-.02em;
              font-variant-numeric:tabular-nums;line-height:1.02}
  .stat-delta{font-size:clamp(20px,2.6vw,32px);font-weight:800;padding:4px 14px;border-radius:12px}
  .stat-delta.up{color:var(--up);background:rgba(43,212,126,.12)}
  .stat-delta.down{color:var(--down);background:rgba(255,92,107,.12)}
  .spark{width:min(340px,40%);height:56px;margin-top:10px;opacity:.9}
  .rows{display:flex;gap:26px;flex-wrap:wrap;margin-top:4px}
  .row{display:flex;align-items:baseline;gap:9px;border:1px solid var(--edge);
       border-radius:12px;padding:9px 14px;background:rgba(11,15,24,.7)}
  .rlabel{color:var(--dim);font-size:11.5px;font-weight:700;letter-spacing:.12em}
  .rvalue{font-weight:700;font-variant-numeric:tabular-nums;font-size:15px}
  .rdelta{font-weight:800;font-size:13px}
  .up{color:var(--up)}.down{color:var(--down)}
  .caption{font-size:clamp(15px,1.9vw,21px);font-weight:650;color:var(--ink);max-width:46ch}
  .vo{font-size:13px;line-height:1.55;color:var(--dim);max-width:72ch}
  footer{position:absolute;left:0;right:0;bottom:34px;padding:0 30px;z-index:3;
         display:flex;align-items:center;gap:16px}
  .dots{display:flex;gap:7px}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--edge);transition:.3s}
  .dot.on{background:var(--accent);box-shadow:0 0 8px var(--accent)}
  .track{flex:1;height:4px;border-radius:4px;background:var(--edge);overflow:hidden}
  .fill{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),#9fd0ff)}
  .tc{font-variant-numeric:tabular-nums;color:var(--dim);font-size:12px;min-width:86px;text-align:right}
  #tape{position:absolute;left:0;right:0;bottom:0;height:30px;border-top:1px solid var(--edge);
        background:rgba(5,7,12,.85);overflow:hidden;white-space:nowrap;z-index:3}
  #tape .inner{display:inline-block;padding-top:6px;animation:scroll 36s linear infinite}
  .titem{margin:0 22px;font-size:12px;color:var(--dim)}
  .titem b{color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}
  @keyframes scroll{from{transform:translateX(0)}to{transform:translateX(-50%)}}
  #playbtn{position:absolute;inset:0;z-index:5;display:flex;align-items:center;justify-content:center;
        background:rgba(5,7,12,.66);backdrop-filter:blur(3px);border:0;cursor:pointer;color:var(--ink)}
  #playbtn .inner-btn{display:flex;flex-direction:column;align-items:center;gap:14px;font:inherit}
  #playbtn .ring{width:92px;height:92px;border-radius:50%;border:2px solid var(--accent);
        display:flex;align-items:center;justify-content:center;font-size:30px;
        background:rgba(110,168,255,.12);box-shadow:0 0 40px rgba(110,168,255,.25)}
  #playbtn .hint{color:var(--dim);font-size:13px;letter-spacing:.12em}
  #wrap .sub{display:flex;justify-content:space-between;color:var(--dim);font-size:12px;
        padding:12px 6px 0;letter-spacing:.06em}
  .hidden{display:none!important}
</style></head>
<body>
<div id="wrap">
  <div id="stage" title="click: play/pause - arrows: skip">
    <header>
      <div class="brand"><i class="sq"></i><span>OKX&nbsp;&nbsp;MARKET BRIEF</span></div>
      <div class="meta"><span class="live">DAILY</span>
        <span>__DATE__</span><span class="badge">__BADGE__</span></div>
    </header>
    __SLIDES__
    <footer>
      <div class="dots">__DOTS__</div>
      <div class="track"><div class="fill" id="fill"></div></div>
      <div class="tc" id="tc">00.0 / __TOTAL__s</div>
    </footer>
    <div id="tape"><div class="inner">__TAPE____TAPE__</div></div>
    <button id="playbtn" class="__PLAY_HIDDEN__"><span class="inner-btn">
      <span class="ring">&#9654;</span>
      <span class="hint">PLAY THE 60-SECOND BRIEF__VOICE_HINT__</span></span></button>
  </div>
  <div class="sub"><span>__STORY__</span><span>auto-generated - data &middot; script &middot; voice &middot; video</span></div>
</div>
__AUDIO_TAG__
<script>
  const TIMING = __TIMING__;
  let TOTAL = __TOTAL__;
  const audio = document.getElementById('vo');
  const slides = [...document.querySelectorAll('.slide')];
  const dots = [...document.querySelectorAll('.dot')];
  const fill = document.getElementById('fill');
  const tc = document.getElementById('tc');
  const playbtn = document.getElementById('playbtn');
  let t = 0, playing = false, last = null, cur = -1;

  function idxFor(time){
    for (const s of TIMING) if (time >= s.start && time < s.end) return s.idx;
    return TIMING.length - 1;
  }
  function render(){
    const i = idxFor(t);
    if (i !== cur){
      cur = i;
      slides.forEach(s => s.classList.toggle('active', +s.dataset.idx === i));
      dots.forEach((d, di) => d.classList.toggle('on', di === i));
    }
    fill.style.width = Math.min(100, t / TOTAL * 100) + '%';
    tc.textContent = t.toFixed(1).padStart(4, '0') + ' / ' + TOTAL + 's';
  }
  function tick(now){
    if (last === null) last = now;
    if (playing && !audio) { t += (now - last) / 1000; if (t >= TOTAL) t = 0; }
    if (audio) t = audio.currentTime;
    last = now; render(); requestAnimationFrame(tick);
  }
  function start(){
    playing = true;
    if (audio) audio.play().catch(() => { /* fall back to timer */ });
    playbtn.classList.add('hidden');
  }
  function toggle(){
    playing = !playing;
    if (audio) playing ? audio.play().catch(()=>{}) : audio.pause();
  }
  function seek(delta){
    const i = Math.max(0, Math.min(TIMING.length - 1, idxFor(t) + delta));
    t = TIMING[i].start + 0.01;
    if (audio) audio.currentTime = t;
    render();
  }
  playbtn.addEventListener('click', e => { e.stopPropagation(); start(); });
  document.getElementById('stage').addEventListener('click', () => {
    if (!playbtn.classList.contains('hidden')) return start();
    toggle();
  });
  document.addEventListener('keydown', e => {
    if (e.code === 'Space'){ e.preventDefault(); playbtn.classList.contains('hidden') ? toggle() : start(); }
    if (e.code === 'ArrowRight') seek(1);
    if (e.code === 'ArrowLeft') seek(-1);
  });
  if (audio){
    // Slides follow the voice: stretch/shrink the estimated timing map to the
    // real mp3 duration so segment boundaries stay in sync with the read.
    audio.addEventListener('loadedmetadata', () => {
      if (audio.duration && isFinite(audio.duration) && audio.duration > 5){
        const k = audio.duration / TOTAL;
        TIMING.forEach(s => { s.start *= k; s.end *= k; });
        TOTAL = Math.round(audio.duration * 10) / 10;
      }
    });
    audio.addEventListener('ended', () => { audio.currentTime = 0; audio.play().catch(()=>{}); });
  } else {
    playing = true; playbtn.classList.add('hidden');  // no voiceover: autoplay timer
  }
  render(); requestAnimationFrame(tick);
</script>
</body></html>
"""


def build_storyboard(script: dict, brief: dict, out_path: Path,
                     audio_path: Path | None = None) -> Path:
    """Write the self-contained storyboard HTML. Embeds the voiceover mp3
    (base64 data URI) when available so the file stays a single artifact."""
    slides_html = "".join(_render_slide(s, brief) for s in script["segments"])
    dots = "".join('<span class="dot"></span>' for _ in script["segments"])
    timing = [{"idx": s["idx"], "start": s["start"], "end": s["end"]}
              for s in script["segments"]]

    audio_tag, voice_hint, play_hidden = "", " (SILENT - NO TTS AUDIO)", ""
    if audio_path and Path(audio_path).exists():
        b64 = base64.b64encode(Path(audio_path).read_bytes()).decode("ascii")
        audio_tag = (f'<audio id="vo" preload="auto" '
                     f'src="data:audio/mpeg;base64,{b64}"></audio>')
        voice_hint = " (WITH VOICEOVER)"

    page = (_HTML_TEMPLATE
            .replace("__TITLE__", f"OKX Market Brief - {script['date']}")
            .replace("__DATE__", esc(script["date"]))
            .replace("__BADGE__", "MOCK DATA" if script["mode"] == "mock" else "LIVE DATA")
            .replace("__SLIDES__", slides_html)
            .replace("__DOTS__", dots)
            .replace("__TAPE__", _tape_items(brief))
            .replace("__STORY__", esc(brief["story_of_the_day"]))
            .replace("__TIMING__", json.dumps(timing))
            .replace("__TOTAL__", str(script["est_total_seconds"]))
            .replace("__AUDIO_TAG__", audio_tag)
            .replace("__VOICE_HINT__", voice_hint)
            .replace("__PLAY_HIDDEN__", play_hidden)
            .replace("__VOICED__", "yes" if audio_tag else "no"))
    out_path.write_text(page, encoding="utf-8")
    return out_path
