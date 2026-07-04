"""x402 pay-per-call gate for the Market Brief Studio service endpoint.

Implements the x402 payment handshake (HTTP 402) in front of ONE route:
POST /api/generate (the listed A2MCP service, 5 USDT per call). Everything
else -- the front page, /briefs static artifacts, /api/health, /api/briefs,
/api/watchlist -- stays free.

Modes (env var X402_MODE, or the `mode` argument):
  off   -> DEFAULT. The gate is transparent: every request passes through
           untouched and no x402 headers are ever added. The server behaves
           exactly as it did before this module existed.
  mock  -> full x402 handshake with an in-process facilitator
           (adapters/facilitator.py). No credentials, no network.
  real  -> same handshake, but verify/settle go to the OKX x402 facilitator
           over HTTPS (see adapters/facilitator.py; needs OKX_X402_* creds).

Handshake (x402 v1, scheme "exact", network X Layer eip155:196):

  1. Client POSTs /api/generate with no payment header.
  2. Server replies HTTP 402 with header
         PAYMENT-REQUIRED: <base64(JSON {x402Version, resource, accepts})>
     -- the FULL challenge object (validators decode the header and read
     `accepts[]` from it; a bare PaymentRequirements object is rejected as
     "accepts is empty") -- and a small JSON body echoing the same challenge.
  3. Client builds a PaymentPayload for the chosen accepts[] entry and
     retries with
         PAYMENT-SIGNATURE: <base64(JSON PaymentPayload)>   (v2, preferred)
     or the legacy v1 form
         X-PAYMENT: <base64(JSON PaymentPayload)>
     (same base64-JSON decode for both; PAYMENT-SIGNATURE wins if both sent).
  4. Server verify()s then settle()s through the facilitator; on success the
     normal generate handler runs and the response carries
         PAYMENT-RESPONSE: <base64(JSON settlement receipt)>
     On verify/settle failure the server replies 402 again with an "error"
     field (and a fresh PAYMENT-REQUIRED challenge).

Stdlib only: base64 / json / os / hashlib -- no pip dependencies.
"""

from __future__ import annotations

import base64
import binascii
import json
import os

from adapters import facilitator

MODE_ENV = "X402_MODE"
DEFAULT_MODE = "off"
MODES = ("off", "mock", "real")

# --- the listed service's price: 5 USDT, in USDT's 6-decimal base units ----
PRICE_USDT = 5
ASSET_DECIMALS = 6
MAX_AMOUNT_REQUIRED = str(PRICE_USDT * 10 ** ASSET_DECIMALS)   # "5000000"

X402_VERSION = 1
SCHEME = "exact"
NETWORK = "eip155:196"          # X Layer mainnet (CAIP-2)
RESOURCE = "/api/generate"
# USDT contract on X Layer:
ASSET = "0x779ded0c9e1022225f8e0630b35a9b54be713736"
PAY_TO_ENV = "X402_PAY_TO"
PAY_TO_PLACEHOLDER = "0xREPLACE_OWNER_WALLET"


# --------------------------------------------------------------------------
# base64(JSON) helpers -- the wire form of every x402 header
# --------------------------------------------------------------------------

def b64_json(obj) -> str:
    """dict -> base64(compact JSON), ASCII str (header-safe)."""
    raw = json.dumps(obj, separators=(",", ":"), sort_keys=True)
    return base64.b64encode(raw.encode("utf-8")).decode("ascii")


def unb64_json(text: str) -> dict:
    """base64(JSON) -> dict. Raises ValueError on anything malformed."""
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise ValueError(f"not valid base64: {e}") from e
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"base64 payload is not JSON: {e}") from e
    if not isinstance(obj, dict):
        raise ValueError("decoded JSON must be an object")
    return obj


# --------------------------------------------------------------------------
# mode + requirements
# --------------------------------------------------------------------------

def resolve_mode(mode: str | None = None) -> str:
    m = (mode or os.environ.get(MODE_ENV) or DEFAULT_MODE).strip().lower()
    if m not in MODES:
        raise ValueError(f"{MODE_ENV} must be one of {MODES}, got {m!r}")
    return m


def build_requirements() -> dict:
    """PaymentRequirements for one call to POST /api/generate (5 USDT)."""
    return {
        "x402Version": X402_VERSION,
        "scheme": SCHEME,
        "network": NETWORK,
        "maxAmountRequired": MAX_AMOUNT_REQUIRED,
        "resource": RESOURCE,
        "description": ("Generate a personalized ~60-second daily market "
                        "video brief"),
        "mimeType": "application/json",
        "payTo": os.environ.get(PAY_TO_ENV, PAY_TO_PLACEHOLDER),
        "maxTimeoutSeconds": 60,
        "asset": ASSET,
        "extra": {"name": "USDT", "decimals": ASSET_DECIMALS},
    }


def build_challenge(requirements: dict) -> dict:
    """The FULL x402 challenge object carried by PAYMENT-REQUIRED.

    Validators decode the header and read `accepts[]` from it, so the wire
    form must be {"x402Version", "resource", "accepts": [PaymentRequirements]}
    -- never a bare PaymentRequirements object.
    """
    return {
        "x402Version": X402_VERSION,
        "resource": requirements["resource"],
        "accepts": [requirements],
    }


def build_mock_payload(requirements: dict,
                       payer: str = "0x" + "d" * 40,
                       nonce: str = "0x" + "11" * 32) -> dict:
    """A well-formed mock PaymentPayload matching `requirements`.

    Used by the demo client and the tests. In real life the buyer's wallet
    produces this (EIP-3009 transferWithAuthorization signature); in mock
    mode the facilitator only checks shape/scheme/network/amount.
    """
    return {
        "x402Version": X402_VERSION,
        "scheme": requirements["scheme"],
        "network": requirements["network"],
        "payload": {
            "signature": "0x" + "ab" * 65,     # mock 65-byte sig
            "authorization": {
                "from": payer,
                "to": requirements["payTo"],
                "value": requirements["maxAmountRequired"],
                "validAfter": "0",
                "validBefore": "99999999999",
                "nonce": nonce,
            },
        },
    }


# --------------------------------------------------------------------------
# the gate
# --------------------------------------------------------------------------

class Verdict:
    """What the HTTP handler should do with a gated request.

    allowed  -> run the normal handler; merge `headers` into the response
                (carries PAYMENT-RESPONSE after a settled payment; empty in
                off mode -- byte-for-byte the pre-x402 behavior).
    !allowed -> reply `status` with JSON `body` and the `headers`
                (carries the PAYMENT-REQUIRED challenge).
    """

    __slots__ = ("allowed", "status", "headers", "body")

    def __init__(self, allowed: bool, status: int = 200,
                 headers: dict | None = None, body: dict | None = None):
        self.allowed = allowed
        self.status = status
        self.headers = headers or {}
        self.body = body or {}


def _challenge(error: str, requirements: dict) -> Verdict:
    return Verdict(
        allowed=False, status=402,
        headers={"PAYMENT-REQUIRED": b64_json(build_challenge(requirements))},
        body={
            "ok": False,
            "x402Version": X402_VERSION,
            "resource": requirements["resource"],
            "error": error,
            "accepts": [requirements],
        })


def _payment_header(headers) -> str | None:
    """Pull the payment header out of a request.

    `headers` may be a mapping of request headers (http.server's
    self.headers, or a plain dict) -- PAYMENT-SIGNATURE (v2, what payment
    tooling replays with) is checked FIRST, falling back to the legacy
    X-PAYMENT -- or a raw header value (str), or None.
    """
    if headers is None or isinstance(headers, str):
        return headers or None
    return (headers.get("PAYMENT-SIGNATURE")
            or headers.get("X-PAYMENT")) or None


def check(headers=None, mode: str | None = None) -> Verdict:
    """Run the x402 handshake for one request to the gated route.

    `headers` is the request-headers mapping (PAYMENT-SIGNATURE is honored
    first, then legacy X-PAYMENT -- same base64-JSON payload either way),
    or a raw header value, or None.
    Never raises for bad *client* input -- that becomes a 402 Verdict.
    Raises ValueError only for a bad X402_MODE (server misconfiguration).
    """
    mode = resolve_mode(mode)
    if mode == "off":                       # transparent passthrough
        return Verdict(allowed=True)

    requirements = build_requirements()
    payment_header = _payment_header(headers)

    if not payment_header:
        return _challenge("payment required: sign an accepts[] entry from "
                          "the PAYMENT-REQUIRED challenge and retry with a "
                          "PAYMENT-SIGNATURE header (or legacy X-PAYMENT), "
                          "base64 JSON PaymentPayload", requirements)

    try:
        payload = unb64_json(payment_header)
    except ValueError as e:
        return _challenge(f"malformed payment header: {e}", requirements)

    try:
        verdict = facilitator.verify(payload, requirements, mode=mode)
    except RuntimeError as e:               # e.g. real mode without creds
        return _challenge(f"payment verification unavailable: {e}",
                          requirements)
    if not verdict.get("isValid"):
        return _challenge("payment rejected: "
                          + str(verdict.get("invalidReason") or "invalid"),
                          requirements)

    try:
        receipt = facilitator.settle(payload, requirements, mode=mode)
    except RuntimeError as e:
        return _challenge(f"payment settlement unavailable: {e}",
                          requirements)
    if not receipt.get("success"):
        return _challenge("payment settlement failed: "
                          + str(receipt.get("errorReason") or "unknown"),
                          requirements)

    return Verdict(allowed=True,
                   headers={"PAYMENT-RESPONSE": b64_json(receipt)})
