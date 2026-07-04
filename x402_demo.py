"""x402 demo client: the full 402 -> pay -> 200 handshake, end to end.

    python x402_demo.py

Against a running Market Brief Studio server (spawned automatically with
X402_MODE=mock on a free port if one isn't already up at X402_DEMO_URL):

  1. POST /api/generate with NO payment      -> HTTP 402
  2. decode the PAYMENT-REQUIRED header      -> full challenge object
     {x402Version, resource, accepts} and pick accepts[0]
  3. build a mock PaymentPayload for it
  4. retry with PAYMENT-SIGNATURE            -> 200 + PAYMENT-RESPONSE receipt
     (the server also still honors the legacy X-PAYMENT header)

The paid call uses {"audio": false} (the pipeline's --no-audio path), so the
demo works even when the TTS rig is down. Stdlib only.

Env:
  X402_DEMO_URL   use an already-running server (e.g. http://localhost:4105)
                  instead of spawning one. That server must have been started
                  with X402_MODE=mock or the first call won't 402.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import x402_gate

HERE = Path(__file__).resolve().parent


# --------------------------------------------------------------------------
# tiny HTTP client (urllib; 402 arrives as HTTPError -- normalize it)
# --------------------------------------------------------------------------

def post_generate(base: str, headers: dict | None = None,
                  body: dict | None = None, timeout: float = 300):
    """POST /api/generate -> (status, headers-dict, json-body)."""
    req = urllib.request.Request(
        base + "/api/generate",
        data=json.dumps(body or {}).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), json.loads(resp.read())
    except urllib.error.HTTPError as e:            # 402/4xx/5xx land here
        raw = e.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"raw": raw.decode(errors="replace")[:400]}
        return e.code, dict(e.headers), payload


def health_ok(base: str) -> bool:
    try:
        with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


# --------------------------------------------------------------------------
# server lifecycle
# --------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def ensure_server() -> tuple[str, subprocess.Popen | None]:
    """(base_url, spawned_process_or_None)."""
    override = os.environ.get("X402_DEMO_URL")
    if override:
        base = override.rstrip("/")
        if not health_ok(base):
            print(f"[demo] X402_DEMO_URL={base} is not answering /api/health")
            sys.exit(1)
        print(f"[demo] using already-running server at {base}")
        return base, None

    port = free_port()
    base = f"http://localhost:{port}"
    env = {**os.environ, "X402_MODE": "mock", "PORT": str(port),
           "HOST": "127.0.0.1"}
    print(f"[demo] spawning server: python serve.py  (X402_MODE=mock, "
          f"port {port})")
    proc = subprocess.Popen(
        [sys.executable, str(HERE / "serve.py")], cwd=str(HERE), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):                             # up to ~10 s
        if health_ok(base):
            return base, proc
        if proc.poll() is not None:
            print("[demo] server exited before it came up")
            sys.exit(1)
        time.sleep(0.2)
    proc.terminate()
    print("[demo] server never answered /api/health")
    sys.exit(1)


# --------------------------------------------------------------------------
# the handshake
# --------------------------------------------------------------------------

def main() -> int:
    base, proc = ensure_server()
    try:
        # -- step 1: call the gated route with no payment ------------------
        print("\n[demo] step 1: POST /api/generate with NO payment header")
        status, headers, body = post_generate(base, body={"audio": False})
        if status != 402:
            print(f"[demo] expected HTTP 402, got {status}: {body}")
            print("[demo] (is the server running with X402_MODE=mock?)")
            return 1
        challenge = headers.get("PAYMENT-REQUIRED")
        print(f"[demo]   -> HTTP 402, error: {body.get('error')}")
        if not challenge:
            print("[demo] 402 response is missing the PAYMENT-REQUIRED header")
            return 1

        # -- step 2: decode the challenge ----------------------------------
        chal = x402_gate.unb64_json(challenge)
        print("[demo] step 2: decoded PAYMENT-REQUIRED challenge:")
        print("         " + json.dumps(chal, indent=2).replace("\n", "\n         "))
        accepts = chal.get("accepts") or []
        if not accepts:
            print("[demo] challenge has no accepts[] -- invalid x402 header")
            return 1
        print(f"[demo]   x402Version {chal.get('x402Version')}, resource "
              f"{chal.get('resource')}, {len(accepts)} accepts[] entr"
              f"{'y' if len(accepts) == 1 else 'ies'} -> using accepts[0]")
        req = accepts[0]
        usdt = int(req["maxAmountRequired"]) / 10 ** req["extra"]["decimals"]
        print(f"[demo]   price: {req['maxAmountRequired']} base units "
              f"= {usdt:g} {req['extra']['name']} on {req['network']}")

        # -- step 3: build a mock PaymentPayload ---------------------------
        payer = "0x" + "de" * 20
        payload = x402_gate.build_mock_payload(req, payer=payer)
        signature = x402_gate.b64_json(payload)
        print(f"[demo] step 3: built mock PaymentPayload for accepts[0] "
              f"(payer {payer})")

        # -- step 4: retry with PAYMENT-SIGNATURE --------------------------
        print("[demo] step 4: retry with PAYMENT-SIGNATURE (audio=false -> "
              "the pipeline's no-audio path; works with TTS down)")
        t0 = time.time()
        status, headers, body = post_generate(
            base, headers={"PAYMENT-SIGNATURE": signature},
            body={"audio": False})
        print(f"[demo]   -> HTTP {status} in {time.time() - t0:.1f}s")
        if status != 200 or not body.get("ok"):
            print(f"[demo] paid call failed: {json.dumps(body, indent=2)}")
            return 1
        receipt_b64 = headers.get("PAYMENT-RESPONSE")
        if not receipt_b64:
            print("[demo] 200 response is missing the PAYMENT-RESPONSE header")
            return 1
        receipt = x402_gate.unb64_json(receipt_b64)
        print("[demo] settlement receipt (decoded PAYMENT-RESPONSE):")
        print("         " + json.dumps(receipt, indent=2)
              .replace("\n", "\n         "))

        # -- where the brief landed ----------------------------------------
        print(f"\n[demo] brief generated for {body['date']} "
              f"(story: {body['story']})")
        print(f"[demo] audio: {body['audio']} ({body['audio_info']})")
        for kind, url in (body.get("artifacts") or {}).items():
            if url:
                print(f"[demo]   {kind:<10} {base}{url}  "
                      f"(file: out\\{url.split('/')[-1]})")
        print("\n[demo] x402 handshake complete: 402 (accepts[] challenge) "
              "-> PAYMENT-SIGNATURE -> 200 with PAYMENT-RESPONSE. Done.")
        return 0
    finally:
        if proc is not None:
            proc.terminate()
            print("[demo] spawned server stopped")


if __name__ == "__main__":
    sys.exit(main())
