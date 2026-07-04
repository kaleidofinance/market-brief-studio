# Deploying Market Brief Studio

Python standard library only — no pip install needed. Railway/Render deploy from
GitHub in a few clicks. `Procfile` (`web: python serve.py`) tells the host how to
start; `requirements.txt` signals a Python project.

## 1. Push to your GitHub

Standalone git repo. Create an empty repo (e.g. `market-brief-studio`), then:

```bash
git remote add origin https://github.com/<your-username>/market-brief-studio.git
git push -u origin main
```

## 2. Create the service (Railway shown; Render is equivalent)

1. railway.app → New Project → **Deploy from GitHub repo** → pick `market-brief-studio`
2. Nixpacks detects Python and runs the `Procfile` start command (`python serve.py`)
3. The server reads `PORT` and binds `0.0.0.0` automatically — no config needed
4. Settings → **Networking → Generate Domain** → this is your public URL

## 3. Environment variables

| Variable | Value | When |
|---|---|---|
| `X402_MODE` | `mock` | now — endpoint demonstrates the full 402 handshake without creds |
| `X402_PAY_TO` | `0xfba093239b764034a37a08758fa6573eea71407f` | now — owner payout wallet (X Layer) |
| `OKX_X402_API_KEY` | from web3.okx.com/onchain-os/dev-portal | before charging real money |
| `OKX_X402_SECRET` | 〃 | 〃 |
| `OKX_X402_PASSPHRASE` | 〃 | 〃 |
| then set `X402_MODE` | `real` | 〃 |

Note: live voice/video generation uses a local TTS engine; the hosted service
degrades gracefully to the silent storyboard when that isn't present. Voice
rendering is a post-listing enhancement, not required for the endpoint or listing.

## 4. Verify

```
https://<your-domain>/api/health   → {"ok":true,...}
https://<your-domain>/             → studio front page
POST https://<your-domain>/api/generate  → 402 challenge (when X402_MODE≠off)
```

## 5. Register the endpoint

Your on-chain service endpoint (permanent) is:

```
https://<your-domain>/api/generate
```
