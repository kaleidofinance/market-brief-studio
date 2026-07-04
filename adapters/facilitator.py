"""x402 facilitator adapter: verify + settle a PaymentPayload.

Mirrors the project's adapter pattern (adapters/okx_data.py, adapters/llm.py):
one entry point per operation, a `mode` switch, fully working mock, and a
documented real implementation that fails fast without credentials.

Modes (env var X402_MODE, or the `mode` argument; "off" never reaches here --
x402_gate short-circuits it):

  mock -> in-process facilitator. verify() checks the payload is well-formed
          (dict from valid base64 JSON -- decoding happens in x402_gate),
          scheme and network match the requirements, and the declared amount
          covers maxAmountRequired. settle() returns a deterministic receipt
          whose transaction hash is sha256 of the canonical payload JSON.
  real -> POST to the OKX x402 facilitator over urllib (stdlib only):
              POST https://web3.okx.com/api/v6/pay/x402/verify
              POST https://web3.okx.com/api/v6/pay/x402/settle
          body {"paymentPayload": ..., "paymentRequirements": ...},
          signed with OKX v5-style HMAC headers from the env vars
          OKX_X402_API_KEY / OKX_X402_SECRET / OKX_X402_PASSPHRASE.
          Raises RuntimeError immediately when any of those is unset.

NOTE on the real mode: the endpoint paths and the header names below are
ASSUMED from OKX's v5 API signing convention -- they are commented inline and
must be confirmed against OKX's x402 facilitator docs before first live call.
OKX also ships official TypeScript SDKs (@okxweb3/x402-core, x402-express,
x402-evm) that wrap this handshake; a Node sidecar using those is the
supported alternative if the raw HTTP contract differs.
"""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request

MODE_ENV = "X402_MODE"

OKX_FACILITATOR_HOST = "https://web3.okx.com"
VERIFY_PATH = "/api/v6/pay/x402/verify"    # ASSUMED path -- confirm vs docs
SETTLE_PATH = "/api/v6/pay/x402/settle"    # ASSUMED path -- confirm vs docs

KEY_ENV = "OKX_X402_API_KEY"
SECRET_ENV = "OKX_X402_SECRET"
PASSPHRASE_ENV = "OKX_X402_PASSPHRASE"

UNKNOWN_PAYER = "0x" + "0" * 40


def _resolve_mode(mode: str | None) -> str:
    m = (mode or os.environ.get(MODE_ENV) or "mock").strip().lower()
    if m not in ("mock", "real"):
        raise ValueError(f"facilitator mode must be mock|real, got {m!r}")
    return m


# --------------------------------------------------------------------------
# payload inspection helpers
# --------------------------------------------------------------------------

def payer_of(payment_payload: dict) -> str:
    """Best-effort payer address from an exact-scheme PaymentPayload."""
    try:
        frm = payment_payload["payload"]["authorization"]["from"]
        if isinstance(frm, str) and frm:
            return frm
    except (KeyError, TypeError):
        pass
    p = payment_payload.get("payer")
    return p if isinstance(p, str) and p else UNKNOWN_PAYER


def declared_amount(payment_payload: dict) -> int | None:
    """Declared payment amount (base units) or None when absent/garbled."""
    try:
        value = payment_payload["payload"]["authorization"]["value"]
        return int(str(value))
    except (KeyError, TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# MOCK facilitator (in-process, deterministic, no network)
# --------------------------------------------------------------------------

def _mock_verify(payment_payload: dict, requirements: dict) -> dict:
    def invalid(reason: str) -> dict:
        return {"isValid": False, "invalidReason": reason,
                "payer": payer_of(payment_payload)}

    if not isinstance(payment_payload, dict):
        return invalid("payment payload must be a JSON object")
    if payment_payload.get("scheme") != requirements["scheme"]:
        return invalid(f"scheme mismatch: expected {requirements['scheme']!r},"
                       f" got {payment_payload.get('scheme')!r}")
    if payment_payload.get("network") != requirements["network"]:
        return invalid(f"network mismatch: expected "
                       f"{requirements['network']!r}, got "
                       f"{payment_payload.get('network')!r}")
    amount = declared_amount(payment_payload)
    required = int(requirements["maxAmountRequired"])
    if amount is None:
        return invalid("missing/invalid payload.authorization.value")
    if amount < required:
        return invalid(f"insufficient amount: declared {amount} < required "
                       f"{required} ({requirements['extra']['name']} base "
                       f"units, {requirements['extra']['decimals']}dp)")
    return {"isValid": True, "invalidReason": None,
            "payer": payer_of(payment_payload)}


def _mock_settle(payment_payload: dict, requirements: dict) -> dict:
    canonical = json.dumps(payment_payload, sort_keys=True,
                           separators=(",", ":"))
    tx = "0x" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return {
        "success": True,
        "transaction": tx,                    # deterministic per payload
        "network": requirements["network"],
        "payer": payer_of(payment_payload),
        "status": "success",
    }


# --------------------------------------------------------------------------
# REAL facilitator (OKX x402 endpoints; documented stub -- no creds on box)
# --------------------------------------------------------------------------

def _okx_creds() -> tuple[str, str, str]:
    key = os.environ.get(KEY_ENV)
    secret = os.environ.get(SECRET_ENV)
    passphrase = os.environ.get(PASSPHRASE_ENV)
    if not (key and secret and passphrase):
        missing = [e for e, v in ((KEY_ENV, key), (SECRET_ENV, secret),
                                  (PASSPHRASE_ENV, passphrase)) if not v]
        raise RuntimeError(
            "X402_MODE=real needs OKX facilitator credentials; missing env "
            f"var(s): {', '.join(missing)}. Set them (OKX dev portal API "
            "key) and re-run, or use X402_MODE=mock. The HTTP contract is "
            "implemented in adapters/facilitator.py:_okx_post.")
    return key, secret, passphrase


def _okx_post(path: str, body_obj: dict) -> dict:
    """Signed POST to the OKX x402 facilitator (raw urllib, no pip deps).

    Signing follows OKX's v5 API convention:
        OK-ACCESS-SIGN = base64(hmac_sha256(timestamp + method + request_path
                                            + body, secret))
        timestamp = ISO-8601 UTC with milliseconds, e.g.
                    2026-07-03T09:00:00.000Z
    The four OK-ACCESS-* header names below are ASSUMED to carry over from
    the v5 REST API to the x402 facilitator -- confirm before going live.
    """
    key, secret, passphrase = _okx_creds()
    body = json.dumps(body_obj, separators=(",", ":"))
    ts = (_dt.datetime.now(_dt.timezone.utc)
          .strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z")
    prehash = ts + "POST" + path + body
    sign = base64.b64encode(
        hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"),
                 hashlib.sha256).digest()).decode("ascii")
    req = urllib.request.Request(
        OKX_FACILITATOR_HOST + path,
        data=body.encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # ASSUMED header names (OKX v5 signing convention):
            "OK-ACCESS-KEY": key,
            "OK-ACCESS-SIGN": sign,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": passphrase,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"OKX facilitator {path} -> HTTP {e.code}: "
                           f"{e.read().decode(errors='replace')[:400]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"OKX facilitator {path} unreachable: "
                           f"{e.reason}") from e
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"OKX facilitator {path} returned non-JSON: "
                           f"{e}") from e


def _real_verify(payment_payload: dict, requirements: dict) -> dict:
    return _okx_post(VERIFY_PATH, {"paymentPayload": payment_payload,
                                   "paymentRequirements": requirements})


def _real_settle(payment_payload: dict, requirements: dict) -> dict:
    return _okx_post(SETTLE_PATH, {"paymentPayload": payment_payload,
                                   "paymentRequirements": requirements})


# --------------------------------------------------------------------------
# public entry points
# --------------------------------------------------------------------------

def verify(payment_payload: dict, requirements: dict,
           mode: str | None = None) -> dict:
    """-> {"isValid": bool, "invalidReason": str|None, "payer": str}."""
    if _resolve_mode(mode) == "mock":
        return _mock_verify(payment_payload, requirements)
    return _real_verify(payment_payload, requirements)


def settle(payment_payload: dict, requirements: dict,
           mode: str | None = None) -> dict:
    """-> settlement receipt: {"success", "transaction", "network",
    "payer", "status"} (mock) / whatever the OKX facilitator returns (real).
    """
    if _resolve_mode(mode) == "mock":
        return _mock_settle(payment_payload, requirements)
    return _real_settle(payment_payload, requirements)
