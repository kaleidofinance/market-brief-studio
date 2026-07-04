"""Market Brief Studio pipeline: data -> script -> voice -> storyboard.

Usage:
    python pipeline.py                # full run (mock mode by default)
    python pipeline.py --demo        # same + loud artifact summary and script
    python pipeline.py --no-audio    # skip the TTS stage
    python pipeline.py --mode real   # real adapters (documented stubs today)
    python pipeline.py --date 2026-07-02

The web studio (`python serve.py`) reuses generate() below for its
POST /api/generate endpoint - one pipeline, two front doors.

Artifacts land in ./out/ :
    brief-<date>.json    the daily data brief
    script-<date>.txt    the broadcast script + timing map + X post
    frames-<date>.json   caption-frame data for video assembly
    brief-<date>.html    self-contained video storyboard (voiceover embedded)
    brief-<date>.mp3     voiceover, when the TTS stage succeeds
    xpost-<date>.txt     <=280-char X post with #okxai
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

import studio
import writer
from adapters import okx_data

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def log(stage: str, msg: str) -> None:
    print(f"[{stage:>10}] {msg}")


class PipelineError(RuntimeError):
    """A stage produced invalid output; .stage names the failing stage."""

    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage


def generate(mode: str | None = None, date: str | None = None,
             no_audio: bool = False, log=None, script_mode: str | None = None) -> dict:
    """Run the pipeline once, in-process: data -> script -> voice -> visuals.

    Shared by the CLI below and by the web studio (`serve.py`, whose
    POST /api/generate calls this). Writes the usual artifacts to ./out/ and
    returns a result dict:

        {"date", "mode", "story", "audio": bool, "audio_info": str,
         "script": {...}, "brief": {...}, "artifacts": {name: filename|None}}

    `mode` selects the DATA adapter (mock | real; real = live OKX public market
    data, no credentials). `script_mode` selects the writer independently
    (defaults to `mode`): the hosted service pins it to "mock" so the template
    writer runs on real numbers without needing ANTHROPIC_API_KEY.

    Raises PipelineError when a stage's validation fails.
    """
    _log = log or (lambda stage, msg: None)
    mode = okx_data.resolve_mode(mode)
    script_mode = okx_data.resolve_mode(script_mode) if script_mode else mode
    date = date or _dt.date.today().isoformat()
    OUT.mkdir(exist_ok=True)
    _log("studio", f"Market Brief Studio - {date} - data={mode} script={script_mode}")

    # 1) Data ---------------------------------------------------------------
    brief = okx_data.get_daily_brief(mode=mode, date=date)
    problems = okx_data.validate_brief(brief)
    if problems:
        raise PipelineError("data", f"INVALID brief: {problems}")
    brief_path = OUT / f"brief-{date}.json"
    brief_path.write_text(json.dumps(brief, indent=2), encoding="utf-8")
    _log("data", f"brief OK - story: {brief['story_of_the_day']}")
    _log("data", f"-> {brief_path}")

    # 2) Script -------------------------------------------------------------
    script = writer.write_script(brief, mode=script_mode)
    problems = writer.validate_script(script)
    if problems:
        raise PipelineError("writer", f"INVALID script: {problems}")
    script_path = OUT / f"script-{date}.txt"
    script_path.write_text(writer.render_script_txt(script, brief), encoding="utf-8")
    xpost_path = OUT / f"xpost-{date}.txt"
    xpost_path.write_text(script["xpost"] + "\n", encoding="utf-8")
    _log("writer", f"{script['word_count']} words, est {script['est_total_seconds']}s, "
                   f"{len(script['segments'])} segments")
    _log("writer", f"-> {script_path}")
    _log("writer", f"-> {xpost_path} ({len(script['xpost'])} chars)")

    # 3) Voice (graceful) ---------------------------------------------------
    audio_path = None
    if no_audio:
        audio_info = "skipped (--no-audio)"
        _log("tts", audio_info)
    else:
        audio_path, audio_info = studio.synthesize(script["full_text"], date, OUT)
        if audio_path:
            _log("tts", f"voiceover OK - {audio_info}")
            _log("tts", f"-> {audio_path}")
        else:
            _log("tts", f"no audio (graceful): {audio_info}")

    # 4) Visuals ------------------------------------------------------------
    frames = studio.build_frames_json(script, brief)
    frames_path = OUT / f"frames-{date}.json"
    frames_path.write_text(json.dumps(frames, indent=2), encoding="utf-8")
    _log("frames", f"-> {frames_path}")

    board_path = studio.build_storyboard(script, brief, OUT / f"brief-{date}.html",
                                         audio_path=audio_path)
    _log("board", f"-> {board_path}  (open in a browser; "
                  f"{'voiceover embedded' if audio_path else 'silent auto-advance'})")

    _, video_info = studio.assemble_video(script, brief, audio_path)
    _log("video", f"mp4 assembly stubbed - {video_info.splitlines()[0]}")

    return {
        "date": date,
        "mode": mode,
        "story": brief["story_of_the_day"],
        "audio": audio_path is not None,
        "audio_info": audio_info,
        "script": script,
        "brief": brief,
        "artifacts": {
            "brief_json": brief_path.name,
            "script_txt": script_path.name,
            "xpost_txt": xpost_path.name,
            "frames_json": frames_path.name,
            "storyboard_html": board_path.name,
            "audio_mp3": audio_path.name if audio_path else None,
        },
    }


def run(mode: str | None, date: str | None, no_audio: bool, demo: bool) -> int:
    try:
        result = generate(mode=mode, date=date, no_audio=no_audio, log=log)
    except PipelineError as e:
        log(e.stage, str(e))
        return 1
    script, date = result["script"], result["date"]

    # Summary ----------------------------------------------------------------
    if demo:
        print("\n" + "=" * 72)
        print("DEMO - every artifact from this run:")
        for p in sorted(OUT.glob(f"*{date}*")):
            print(f"  {p}  ({p.stat().st_size:,} bytes)")
        print("\nTHE SCRIPT:")
        for s in script["segments"]:
            print(f"  [{s['start']:05.1f}s] {s['kind'].upper():<9} {s['text']}")
        print("\nX POST:")
        print("  " + script["xpost"].replace("\n", "\n  "))
        print("\nWatch it: open out/brief-" + date + ".html in a browser.")
        print("=" * 72)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Auto-generated 60-second daily market recap")
    ap.add_argument("--mode", choices=["mock", "real"], default=None,
                    help="adapter mode (default: env OKX_MODE or 'mock')")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")
    ap.add_argument("--no-audio", action="store_true", help="skip the TTS stage")
    ap.add_argument("--demo", action="store_true",
                    help="run the full mock pipeline and print every artifact")
    args = ap.parse_args(argv)
    if args.demo and args.mode is None:
        args.mode = "mock"
    try:
        return run(args.mode, args.date, args.no_audio, args.demo)
    except NotImplementedError as e:  # real-mode stubs land here
        print(f"\n[stub] {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
