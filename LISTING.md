# OKX.AI Listing Manifest — Market Brief Studio

The submission record for listing this agent on OKX.AI. Listing is an **on-chain
identity + service registration on X Layer** via the Onchain OS CLI — not a web
form. Fill the two `REPLACE_*` placeholders, then run the command sequence below.

## Canonical manifest

```json
{
  "role": "asp",
  "identity": {
    "name": "Market Brief Studio",
    "description": "Market Brief Studio generates a fresh 60-second market recap every day — pulling live prices, top movers, funding and news sentiment, writing a broadcast-style script, and rendering it to voice and an on-screen visual brief. Set your watchlist and get a daily video recap that makes itself. Nobody reads market recaps; everybody watches them.",
    "avatar_file": "./brand/avatar.png",
    "preferred_language": "en"
  },
  "services": [
    {
      "name": "Daily Market Video Brief",
      "description": "Produces a daily ~60-second market recap: script, AI voiceover and a visual storyboard covering majors, top movers, funding and sentiment, plus a ready-to-post caption. You supply: your watchlist symbols and preferred language; delivery by link or feed. Personalized daily market media, generated hands-free every morning.",
      "type": "A2MCP",
      "fee": "5",
      "fee_currency": "USDT",
      "endpoint": "https://REPLACE_WITH_YOUR_DEPLOY_HOST/api/generate"
    }
  ]
}
```

- **`REPLACE_WITH_YOUR_DEPLOY_HOST`** → your deployed domain. The local route is
  `POST /api/generate` (see [STATUS.md](STATUS.md)); must be a public `https://`
  URL (permanent on-chain).
- **`avatar.png`** → required uploaded image. Broadcast-studio identity: hot
  red-orange "ON AIR" mic / clapperboard motif on dark. Put it at `brand/avatar.png`.
- **fee** `"5"` = 5 USDT / month for a personalized daily brief. Adjust freely;
  digits only, ≤6 decimals, currency is USDT.

## Registration command sequence

```bash
# 0. Wallet session (TEE) — identities live on X Layer only, never pass --chain
onchainos wallet status --format json
onchainos wallet login <your-email>        # then: onchainos wallet verify <code>

# 1. Consent / eligibility (one ASP identity per wallet)
onchainos agent pre-check --role asp

# 2. Upload the avatar, capture the returned URL for --picture
onchainos agent upload --file ./brand/avatar.png

# 3. Automated listing QA — fix any findings before create
onchainos agent validate-listing --role asp \
  --name "Market Brief Studio" \
  --description "Market Brief Studio generates a fresh 60-second market recap every day — pulling live prices, top movers, funding and news sentiment, writing a broadcast-style script, and rendering it to voice and an on-screen visual brief. Set your watchlist and get a daily video recap that makes itself. Nobody reads market recaps; everybody watches them." \
  --service '[{"name":"Daily Market Video Brief","description":"Produces a daily ~60-second market recap: script, AI voiceover and a visual storyboard covering majors, top movers, funding and sentiment, plus a ready-to-post caption. You supply: your watchlist symbols and preferred language; delivery by link or feed. Personalized daily market media, generated hands-free every morning.","type":"A2MCP","fee":"5","endpoint":"https://REPLACE_WITH_YOUR_DEPLOY_HOST/api/generate"}]'

# 4. Create the on-chain identity → returns newAgentId
onchainos agent create --role asp \
  --name "Market Brief Studio" \
  --description "<same description as above>" \
  --picture "<url from step 2>" \
  --service '<same --service JSON as above>'

# 5. Activate → submits for review / publishes
onchainos agent activate --agent-id <newAgentId> --preferred-language en
```

On-chain fees are covered by OKX (X Layer is gas-free). Settlement is in USDT.

## Owner values (Jul 3, 2026)

| Field | Value |
|---|---|
| Owner email / wallet login | quyyumibidun@gmail.com |
| Payout wallet (X Layer, `X402_PAY_TO`) | `0xfba093239b764034a37a08758fa6573eea71407f` |
| Avatar (uploaded) | `https://static.okx.com/cdn/web3/wallet/marketplace/headimages/agent/avatar/7241185f-2864-4dee-abb0-bb6d4ab20f42.png` |
| Consent | accepted; `pre-check --role asp` → `canCreate: true` |
| Endpoint | pending deploy — see [DEPLOY.md](DEPLOY.md) |

**Registration schema (proven):** `--service` keys camelCase; `serviceDescription`
two lines; endpoint must pass `x402-check`; re-upload avatar immediately before
`create`; Railway needs `HOST=0.0.0.0` (fixed in serve.py + Procfile).

## Owner values — REGISTERED (Jul 3, 2026)

| Field | Value |
|---|---|
| **Agent ID** | **3630** (X Layer, chain 196) |
| Registration tx | `0x0fa34cf9d66c26bd5fc39f781b4236f0e9da14919af707faa4976169676cf0da` |
| Status | **submitted for review** (`approvalStatus: 2`); result → owner email in ~2 business days |
| Owner email / wallet login | quyyumibidun@gmail.com |
| Payout wallet (X Layer, `X402_PAY_TO`) | `0xfba093239b764034a37a08758fa6573eea71407f` |
| Avatar (uploaded) | `https://static.okx.com/cdn/web3/wallet/marketplace/headimages/agent/avatar/2a4af233-8574-48e5-a795-2bdc87031192.png` |
| Endpoint (on-chain, permanent) | `https://market-brief-studio-production.up.railway.app/api/generate` — passes `x402-check` |
| Repo | github.com/kaleidofinance/market-brief-studio (commit author: kaleidofinance, verified) |

## Owner checklist

- [x] x402 pay-per-call built on `POST /api/generate` (HTTP 402 handshake,
      5 USDT = `"5000000"` 6dp base units, X Layer `eip155:196` USDT) —
      **mock facilitator verified end-to-end** (`tests.py` 15/15 +
      `x402_demo.py`); real OKX facilitator creds (`OKX_X402_*`) + endpoint
      confirmation still pending. Run with `X402_MODE=mock|real`; set
      `X402_PAY_TO` to the owner wallet. See STATUS.md "x402 payment layer".
- [ ] Deploy the service; set the real `https://` endpoint (replace the placeholder)
- [x] Create `brand/avatar.png` — done (1024×1024, ON AIR studio mic in red-orange on dark; editable source at `brand/avatar.svg`)
- [ ] Register hackathon + OKX Onchain OS dev-portal creds (`.env`)
- [ ] Run steps 0-5 above; record `newAgentId`
- [ ] Confirm activation status (submitApproval → under review)
- [ ] Submit the hackathon Google form before **Jul 17 00:00 UTC**
- [ ] Post the ≤90s demo on X with **#okxai**
