# STATUS — Market Brief Studio

_Last verified by running end-to-end on this machine: 2026-07-02 (Python 3.14.3, Windows)._

## What works right now (verified, not aspirational)

`python pipeline.py --demo` runs the full mock pipeline end-to-end and produced,
on this machine, with **real TTS audio**:

| Artifact | What it is | Status |
|---|---|---|
| `out/brief-<date>.json`  | daily data brief (majors, top-3 gainers/losers, funding, sentiment) | WORKS (mock data) |
| `out/script-<date>.txt`  | ~150-word broadcast script + per-segment timing map + X post | WORKS (mock template writer) |
| `out/brief-<date>.mp3`   | voiceover (en-US-GuyNeural via the existing `tts_server.py`) | **WORKS — real audio, wired + verified** (needs internet) |
| `out/brief-<date>.html`  | self-contained video storyboard: dark theme, big numbers, sparklines, ticker tape, auto-advancing slides synced to the timing map, **voiceover embedded as base64** | WORKS (screenshot-verified in headless Edge) |
| `out/frames-<date>.json` | caption-frame data (start/end/caption/stat per segment) — the hand-off format for mp4 assembly | WORKS |
| `out/xpost-<date>.txt`   | ≤280-char X post with `#okxai` | WORKS |
| actual `.mp4` file       | via `video_server.py` | **STUBBED** (see below) |

Tests: `python tests.py` → **16/16 pass** (timing-map contiguity + pace model,
data validation/consistency, mock determinism per date, real-numbers
interpolation, xpost constraints, storyboard rendering, real-mode stub errors,
plus the 6 x402 gate tests below).

## What is mocked vs real

| Layer | mock (default) | real (`OKX_MODE=real` or `--mode real`) |
|---|---|---|
| Market data (`adapters/okx_data.py`) | deterministic-per-date snapshot with an internally consistent "story of the day" (SOL leads on ETF inflows, memes bleed, funding matches) | **documented stub** — raises with the exact `okx` CLI commands (verified against the public `okx/agent-skills` repo: each skill's `SKILL.md` plus the CLI's own `list-tools` schema); see `REAL_COMMANDS` |
| Script writer (`adapters/llm.py`) | template-based writer interpolating the real brief numbers; 6 hooks / 3 outros seeded by date | **implemented but unexercised stub** — raw `urllib` POST to `https://api.anthropic.com/v1/messages`, model `claude-opus-4-8`, adaptive thinking; raises immediately if `ANTHROPIC_API_KEY` is unset (it is, on this box) |
| TTS | — (no mock; degrades gracefully) | **real and working** through `tts_server.py` |
| Video mp4 | — | stub with documented integration steps |

## TTS server findings (`C:/Users/USER/tts_server.py` — inspected, unmodified)

- **API**: `POST http://localhost:8765` with JSON `{"text": "...", "video_id": "..."}`.
  Response: `{"success": true, "audio_path": "C:/Users/USER/audio_<video_id>.mp3"}`
  or `{"success": false, "error": "<edge-tts stderr>"}`.
- **Engine**: shells out to `edge-tts --voice en-US-GuyNeural` (pip package
  `edge-tts` 7.2.7 is installed here). Microsoft **online** voices — needs
  internet; it can be flaky, so the client retries once (observed one transient
  timeout across four runs).
- **No health route**: any POST triggers a synthesis, GET is unhandled, so
  liveness is a TCP connect to `:8765`.
- **Lifecycle**: the server was not running; `studio.synthesize()` starts it
  (`python C:/Users/USER/tts_server.py`, no window) and terminates it after the
  run if it was the one to start it.
- **Output location**: the server hardcodes `C:/Users/USER/audio_<video_id>.mp3`;
  the pipeline copies that into `out/brief-<date>.mp3` (the server's own copy
  stays where the server always writes it — this project never writes outside
  `market-brief-studio/`).
- **Measured pace**: 149 words → 76.5 s of audio (~2.0 words/sec incl. pauses).
  The writer's timing map is calibrated to that (`writer.WPS = 2.0`), and the
  storyboard additionally rescales its map to the actual mp3 duration in JS
  (`loadedmetadata`), so slides stay in sync with the voice.
- Fallback: `--no-audio`, missing `edge-tts`, server-start failure, or an
  offline box all degrade gracefully to the silent auto-advancing storyboard.

## Video server findings (`C:/Users/USER/video_server.py` — inspected, unmodified)

- **API**: `POST http://localhost:8766`
  - `{"action": "draw_scenes", "video_id", "scenes": [...], "frames_per_scene": [...]}`
    → PIL-drawn frames in `C:/Users/USER/videos/<id>/frames` + `frames_list.json`
  - `{"action": "build_video", "video_id", "title", "sound_cues", "scenes"}`
    → ffmpeg concat @ 8 fps, optional sound cues from `C:/Users/USER/sounds/`,
    output `C:/Users/USER/videos/<id>_<title>.mp4`
- **Why mp4 assembly is stubbed**, not wired:
  1. Its renderer draws **stickman narrative scenes** (battlefield/castle
     settings, characters with actions/expressions) — the wrong visual language
     for a market brief; there is no caption/number frame mode.
  2. `build_video` only consumes frames that its own `draw_scenes` wrote under
     `C:/Users/USER/videos/<id>/`; injecting our frames there is outside this
     project's allowed write path (`market-brief-studio/` only).
- **Documented direct path** (ffmpeg IS installed at `C:\ffmpeg\...`), kept in
  `studio.VIDEO_INTEGRATION_STEPS` and printed by the pipeline:
  1. render each entry of `out/frames-<date>.json` to a 1280×720 PNG (Pillow
     12.1.1 is installed) in `out/frames/`;
  2. `ffmpeg -f concat -safe 0 -i filelist.txt -c:v libx264 -pix_fmt yuv420p -r 30 out/brief-<date>-silent.mp4`
     with per-frame `duration` lines taken from the timing map;
  3. `ffmpeg -i silent.mp4 -i out/brief-<date>.mp3 -c:v copy -c:a aac -shortest out/brief-<date>.mp4`.

## How to run

```powershell
cd C:\Users\USER\OKX\market-brief-studio
python pipeline.py --demo        # full mock pipeline + artifact summary
python pipeline.py               # same, quiet
python pipeline.py --no-audio    # skip TTS (silent storyboard)
python pipeline.py --date 2026-07-03
python tests.py                  # 10 tests, plain asserts
# then open out\brief-<date>.html in a browser (click once to start the voiceover)
```

No pip installs needed for the pipeline itself (stdlib only). TTS additionally
needs `edge-tts` on PATH (already installed) + internet.

## Wiring real mode (when credentials exist)

1. **Data**: `npm install -g @okx_ai/okx-trade-cli`, then implement
   `adapters/okx_data.py:_real_daily_brief` around `REAL_COMMANDS` (market data
   needs no API keys; `okx news ...` needs `~/.okx/config.toml` with a live
   profile). Every command is already spelled out in the stub's error message.
2. **Writer**: `set ANTHROPIC_API_KEY=...` — the Claude call in
   `adapters/llm.py:_claude_segments` is already implemented (raw urllib,
   no SDK dependency) and validates the returned segment JSON.
3. Run `python pipeline.py --mode real`.

## x402 payment layer (pay-per-call on POST /api/generate)

The listed A2MCP service (5 USDT per call, see LISTING.md) now implements the
x402 HTTP-402 payment handshake. **Verified end to end in mock mode on this
machine (2026-07-03): `python tests.py` 16/16, `python x402_demo.py`
402 → pay → 200 with a settled receipt and a generated brief.**

**Files**: `x402_gate.py` (the gate + PaymentRequirements/base64 helpers),
`adapters/facilitator.py` (verify/settle, mock + real, same adapter pattern
as `okx_data.py`/`llm.py`), `x402_demo.py` (demo buyer client).

**Env vars**

| Env | Meaning |
|---|---|
| `X402_MODE` | `off` (default) \| `mock` \| `real`. `off` = gate fully transparent: server behaves byte-for-byte as before (verified live). |
| `X402_PAY_TO` | owner wallet for `payTo` in the challenge (default placeholder `0xREPLACE_OWNER_WALLET`) |
| `OKX_X402_API_KEY` / `OKX_X402_SECRET` / `OKX_X402_PASSPHRASE` | OKX facilitator creds, **real mode only**; missing → immediate clear RuntimeError (surfaced as a 402 with the message) |

**Flow** (only `POST /api/generate` is gated; `/`, `/briefs/*`, `/api/health`,
`/api/briefs`, `/api/watchlist` stay free):

1. `POST /api/generate` with no payment header → **HTTP 402** + header
   `PAYMENT-REQUIRED: base64(JSON {x402Version: 1, resource: "/api/generate",
   accepts: [PaymentRequirements]})` — the **full challenge object**, not a
   bare PaymentRequirements (validators decode the header and read
   `accepts[]`; a bare object is rejected as "accepts is empty"). The
   `accepts[0]` entry: x402 v1, scheme `exact`, network `eip155:196` =
   X Layer, `maxAmountRequired` `"5000000"` = 5 USDT × 10⁶ at the USDT
   contract `0x779ded0c9e1022225f8e0630b35a9b54be713736`, 6 decimals. The 402
   JSON body echoes the same challenge:
   `{"ok": false, "x402Version": 1, "resource", "error", "accepts": [...]}`.
2. Buyer signs the chosen `accepts[]` entry and retries with
   `PAYMENT-SIGNATURE: base64(JSON PaymentPayload)` (v2, checked **first**)
   or the legacy `X-PAYMENT: base64(JSON PaymentPayload)` (still supported;
   same base64-JSON decode for both) → facilitator `verify()` then
   `settle()`; success → the normal generate handler runs and the response
   carries `PAYMENT-RESPONSE: base64(JSON receipt)`
   (`{"success", "transaction", "network", "payer", "status"}`).
3. Any verify/settle failure → 402 again with an `error` field and a fresh
   challenge header.

**Confirmed vs assumed**

- CONFIRMED (built + tested here): the whole mock path — challenge shape,
  base64 wire format, amount math (6dp), verify rules (scheme/network/amount),
  deterministic settle (`transaction` = `0x` + sha256 of the canonical
  payload JSON), off-mode passthrough.
- ASSUMED (commented in `adapters/facilitator.py`, unexercised — no creds):
  real-mode facilitator endpoints
  `POST https://web3.okx.com/api/v6/pay/x402/verify` / `.../settle` with body
  `{"paymentPayload":..., "paymentRequirements":...}`, and the OKX v5-style
  HMAC signing headers (`OK-ACCESS-KEY`, `OK-ACCESS-SIGN` =
  base64(hmac_sha256(timestamp+method+path+body, secret)),
  `OK-ACCESS-TIMESTAMP`, `OK-ACCESS-PASSPHRASE`). Confirm both against the
  OKX x402 docs before first live call. Alternative: OKX's official
  TypeScript SDKs (`@okxweb3/x402-core`, `x402-express`, `x402-evm`) as a
  Node sidecar if the raw HTTP contract differs.

**Demo**: `python x402_demo.py` — spawns the server with `X402_MODE=mock` on
a free port (or uses `X402_DEMO_URL`), shows 402 → decoded challenge
(`accepts[0]`) → mock payment via `PAYMENT-SIGNATURE` → 200 + decoded
receipt + artifact URLs. Uses `{"audio": false}`
(the pipeline's no-audio path) so it works with the TTS rig down.

## Known limitations / next steps

- mp4 assembly is the one unfinished studio stage (steps documented above).
- The mock brief is one canonical "story of the day" with per-date jitter;
  more story archetypes would add variety day-over-day.
- Phases 4–6 of the README (publish cron, X/Telegram delivery, marketplace
  listing) are not started.
