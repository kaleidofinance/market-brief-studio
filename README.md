# Market Brief Studio

**Auto-generated 60-second daily market recap in voice + video.** Pulls OKX market data + sentiment, writes a tight script, renders it to audio/video, and delivers/posts it daily.

## Target: Best Product (+ Art Creation side category)

The demo *is* the product — a 90-second video that generates itself fresh every morning. Reuses working TTS and video-generation servers already on this machine (`~/tts_server.py`, `~/video_server.py`), so it's mostly assembly.

## Revenue model

- Subscription: personal daily brief (your watchlist, your language)
- Sponsor slot in the free public daily brief
- B2B: white-label briefs for communities/KOLs

## How it works

```
┌─ Data pull (daily cron) ─┐   market-data skill: tickers,
│ top movers, BTC/ETH,     │   candles, funding
│ funding, news sentiment  │   sentiment skill: news/social
└──────────┬───────────────┘
           ▼
┌─ Script writer ──────────┐
│ Claude: 150-word brief,  │
│ tight broadcast style    │
└──────────┬───────────────┘
           ▼
┌─ Studio ─────────────────┐
│ TTS render (existing     │
│ tts_server.py)           │
│ video assembly: charts + │
│ captions (video_server)  │
└──────────┬───────────────┘
           ▼
┌─ Publish ────────────────┐
│ X post / Telegram /      │
│ subscriber delivery      │
└──────────────────────────┘
```

## Stack

- Python for the studio pipeline (reuse `tts_server.py`, `video_server.py`)
- `okx/agent-skills` market-data + sentiment skills for the data layer
- Claude for scriptwriting
- Chart frames: candlestick snapshots of the day's movers baked into the video

## Build plan

- [x] Phase 1 — Data: daily pull of movers, majors, funding, sentiment into one JSON brief
      *(done in mock mode — `adapters/okx_data.py`, validated + deterministic per date; real mode is a stub documenting the exact `okx-trade-cli` commands)*
- [x] Phase 2 — Script: 150-word broadcast tone (hook → 3 stories → outro) + per-segment timing map
      *(done — `writer.py`; mock = template writer interpolating the real numbers; the Claude call for real mode is implemented in `adapters/llm.py` but needs `ANTHROPIC_API_KEY`)*
- [x] Phase 3a — Studio, voice: existing TTS server wired and **verified working** (`out/brief-<date>.mp3`, en-US-GuyNeural; graceful `--no-audio` fallback)
- [x] Phase 3b — Studio, visuals: self-contained HTML video storyboard with charts + captions synced to the timing map, voiceover embedded (`out/brief-<date>.html`)
- [ ] Phase 3c — Studio, mp4: assembly via `video_server.py` stubbed — its stickman renderer doesn't fit; direct ffmpeg path documented in `STATUS.md`
- [ ] Phase 4 — Publish: daily cron → X + Telegram; personal-watchlist variant for subscribers
      *(the ≤280-char `#okxai` post text is already generated at `out/xpost-<date>.txt`)*
- [ ] Phase 5 — Marketplace: ASP listing (personal brief subscription + white-label service)
- [ ] Phase 6 — Traction: post the brief daily with #okxai starting now — each one is a demo
- [ ] Submit Google form (after listing, before Jul 17 00:00 UTC)
- [ ] Post demo on X with #okxai

## Run it

```powershell
cd C:\Users\USER\OKX\market-brief-studio
python pipeline.py --demo    # data -> script -> voice -> storyboard, prints every artifact
python tests.py              # 10 tests (timing map, data validation, storyboard, stubs)
```

Then open `out\brief-<date>.html` in a browser — click once to play the voiced
brief. Mock mode is the default and fully self-contained; see `STATUS.md` for
what's real vs mocked and how to wire live OKX data + Claude.

## Demo script (≤90s)

1. (0–10s) "Nobody reads market recaps. Everybody watches them."
2. (10–70s) Play one actual generated brief (that day's real data — charts, voice, captions)
3. (70–90s) "Generated end-to-end by an agent this morning: data → script → voice → video. Yours, on your watchlist, every day."
