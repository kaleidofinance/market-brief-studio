"""Tests for Market Brief Studio (plain asserts - run: python tests.py).

Covers the writer's timing map, data validation, mock determinism, xpost
constraints, storyboard rendering, the real-mode stubs' error contracts, and
the x402 pay-per-call gate (off-mode passthrough, full-challenge header
shape, amount math, mock verify/settle round trip via PAYMENT-SIGNATURE,
legacy X-PAYMENT support, bad-payment rejection).
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from pathlib import Path

import studio
import writer
import x402_gate
from adapters import facilitator, llm, okx_data

HERE = Path(__file__).resolve().parent
DATE = "2026-07-02"


# --------------------------------------------------------------------------
def test_mock_brief_valid():
    brief = okx_data.get_daily_brief(mode="mock", date=DATE)
    problems = okx_data.validate_brief(brief)
    assert problems == [], f"brief invalid: {problems}"


def test_mock_brief_deterministic_per_date():
    a = okx_data.get_daily_brief(mode="mock", date=DATE)
    b = okx_data.get_daily_brief(mode="mock", date=DATE)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True), \
        "same date must produce identical briefs"
    c = okx_data.get_daily_brief(mode="mock", date="2026-07-03")
    assert json.dumps(a, sort_keys=True) != json.dumps(c, sort_keys=True), \
        "different dates should produce different briefs"


def test_brief_internal_consistency():
    brief = okx_data.get_daily_brief(mode="mock", date=DATE)
    g = [r["chg24h_pct"] for r in brief["gainers"]]
    l = [r["chg24h_pct"] for r in brief["losers"]]
    assert all(x > 0 for x in g) and g == sorted(g, reverse=True)
    assert all(x < 0 for x in l) and l == sorted(l)
    for inst in ("BTC-USDT", "ETH-USDT"):
        m = brief["majors"][inst]
        assert m["low24h"] <= m["last"] <= m["high24h"], f"{inst} range broken"
    assert all(f["inst"].endswith("-SWAP") for f in brief["funding"])
    assert 2 <= len(brief["sentiment"]["items"]) <= 3
    # the story of the day is backed by the data: top gainer has a bullish
    # sentiment item and hot funding; top loser has a bearish item
    g1 = llm.base_sym(brief["gainers"][0]["inst"])
    l1 = llm.base_sym(brief["losers"][0]["inst"])
    coins_bull = {c for i in brief["sentiment"]["items"]
                  if i["sentiment"] == "bullish" for c in i["coins"]}
    coins_bear = {c for i in brief["sentiment"]["items"]
                  if i["sentiment"] == "bearish" for c in i["coins"]}
    assert g1 in coins_bull, f"top gainer {g1} lacks a bullish catalyst item"
    assert l1 in coins_bear, f"top loser {l1} lacks a bearish catalyst item"


# --------------------------------------------------------------------------
def _script():
    brief = okx_data.get_daily_brief(mode="mock", date=DATE)
    return brief, writer.write_script(brief, mode="mock")


def test_script_shape_and_word_target():
    _, script = _script()
    assert [s["kind"] for s in script["segments"]] == llm.SEGMENT_KINDS
    lo, hi = writer.WORD_TARGET
    assert lo <= script["word_count"] <= hi, \
        f"word count {script['word_count']} outside {writer.WORD_TARGET}"
    assert writer.validate_script(script) == []


def test_timing_map_contiguous_and_plausible():
    _, script = _script()
    segs = script["segments"]
    assert segs[0]["start"] == 0.0
    for prev, nxt in zip(segs, segs[1:]):
        assert abs(prev["end"] - nxt["start"]) < 0.05, \
            f"timeline gap {prev['kind']}->{nxt['kind']}"
    for s in segs:
        assert s["duration"] >= writer.MIN_SEG_SECONDS
        assert abs((s["end"] - s["start"]) - s["duration"]) < 0.05
        expected = max(writer.MIN_SEG_SECONDS,
                       s["words"] / writer.WPS + writer.SEG_PAUSE)
        assert abs(s["duration"] - expected) < 0.11, \
            f"{s['kind']} duration {s['duration']} != words/wps model {expected:.1f}"
    lo_s, hi_s = writer.RUNTIME_BOUNDS
    assert lo_s <= script["est_total_seconds"] <= hi_s, \
        f"total {script['est_total_seconds']}s outside {writer.RUNTIME_BOUNDS}"
    assert script["est_total_seconds"] == segs[-1]["end"]


def test_script_uses_real_numbers():
    brief, script = _script()
    text = script["full_text"]
    btc = brief["majors"]["BTC-USDT"]
    assert llm.spoken_price(btc["last"]) in text, "BTC price not spoken"
    assert str(abs(btc["chg24h_pct"])) in text, "BTC % change not spoken"
    assert llm.base_sym(brief["gainers"][0]["inst"]) in text
    assert llm.base_sym(brief["losers"][0]["inst"]) in text


def test_xpost_constraints():
    brief, script = _script()
    post = script["xpost"]
    assert len(post) <= 280, f"xpost {len(post)} chars"
    assert "#okxai" in post
    assert llm.fmt_price(brief["majors"]["BTC-USDT"]["last"]) in post


# --------------------------------------------------------------------------
def test_storyboard_renders_every_segment():
    brief, script = _script()
    out = HERE / "out"
    out.mkdir(exist_ok=True)
    path = out / "_test_storyboard.html"
    try:
        studio.build_storyboard(script, brief, path, audio_path=None)
        html_text = path.read_text(encoding="utf-8")
        assert "__TIMING__" not in html_text and "__SLIDES__" not in html_text, \
            "template placeholders left unexpanded"
        for s in script["segments"]:
            assert studio.esc(s["caption"]) in html_text, \
                f"caption for {s['kind']} missing from storyboard"
            assert studio.esc(s["text"]) in html_text, \
                f"spoken text for {s['kind']} missing from storyboard"
        timing = json.loads(
            html_text.split("const TIMING = ", 1)[1].split(";", 1)[0])
        assert len(timing) == len(script["segments"])
        assert timing[0]["start"] == 0.0
        assert timing[-1]["end"] == script["est_total_seconds"]
    finally:
        path.unlink(missing_ok=True)


def test_frames_json_matches_timing_map():
    brief, script = _script()
    frames = studio.build_frames_json(script, brief)
    assert frames["total_seconds"] == script["est_total_seconds"]
    assert [f["start"] for f in frames["frames"]] == \
           [s["start"] for s in script["segments"]]
    assert all(f["caption"] and f["spoken"] for f in frames["frames"])


# --------------------------------------------------------------------------
def test_real_mode_stubs_fail_loudly_and_helpfully():
    # Real DATA mode is wired to live OKX public market data and is endpoint-safe:
    # it returns a VALID brief either way — real when the API is reachable, or a
    # deterministic mock fallback (tagged with `real_error`) on any failure.
    brief = okx_data.get_daily_brief(mode="real", date=DATE)
    assert okx_data.validate_brief(brief) == [], "real-mode brief must be valid"
    assert brief["mode"] in ("real", "mock")
    if brief["mode"] == "mock":
        assert "real_error" in brief, "a fallback must record why real failed"

    key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        brief = okx_data.get_daily_brief(mode="mock", date=DATE)
        try:
            llm.generate_segments(brief, mode="real")
            assert False, "real LLM mode without a key should raise"
        except RuntimeError as e:
            assert "ANTHROPIC_API_KEY" in str(e)
    finally:
        if key is not None:
            os.environ["ANTHROPIC_API_KEY"] = key


# --------------------------------------------------------------------------
# x402 pay-per-call gate
# --------------------------------------------------------------------------

def test_x402_off_mode_is_a_transparent_passthrough():
    """Default (X402_MODE unset/off): the gate never blocks and never adds
    headers -- /api/generate behaves exactly as before x402 existed."""
    saved = os.environ.pop("X402_MODE", None)
    try:
        for header in (None, "", "garbage-not-base64",
                       x402_gate.b64_json({"anything": 1}),
                       {"PAYMENT-SIGNATURE": "whatever"},
                       {"X-PAYMENT": "whatever"}):
            v = x402_gate.check(header)
            assert v.allowed, f"off mode must pass through (header={header!r})"
            assert v.headers == {}, "off mode must not add x402 headers"
        os.environ["X402_MODE"] = "off"          # explicit off, same thing
        v = x402_gate.check(None)
        assert v.allowed and v.headers == {}
    finally:
        os.environ.pop("X402_MODE", None)
        if saved is not None:
            os.environ["X402_MODE"] = saved


def test_x402_challenge_shape_and_base64_decode():
    v = x402_gate.check(None, mode="mock")       # no payment -> challenge
    assert not v.allowed and v.status == 402
    assert "PAYMENT-REQUIRED" in v.headers
    # the header carries the FULL challenge object -- validators decode it
    # and read accepts[]; a bare PaymentRequirements object is invalid
    chal = x402_gate.unb64_json(v.headers["PAYMENT-REQUIRED"])
    assert chal["x402Version"] == 1
    assert chal["resource"] == "/api/generate"
    assert isinstance(chal["accepts"], list) and len(chal["accepts"]) == 1, \
        "PAYMENT-REQUIRED must decode to a challenge with a non-empty accepts[]"
    req = chal["accepts"][0]                     # the PaymentRequirements
    assert req["x402Version"] == 1
    assert req["scheme"] == "exact"
    assert req["network"] == "eip155:196"
    assert req["resource"] == "/api/generate"
    assert req["mimeType"] == "application/json"
    assert req["asset"] == "0x779ded0c9e1022225f8e0630b35a9b54be713736"
    assert req["maxTimeoutSeconds"] == 60
    assert req["extra"] == {"name": "USDT", "decimals": 6}
    assert req["payTo"]                          # env override or placeholder
    # the JSON body mirrors the challenge and carries an error field
    assert v.body["ok"] is False
    assert v.body["x402Version"] == 1
    assert v.body["resource"] == "/api/generate"
    assert v.body["error"] and v.body["accepts"] == [req]


def test_x402_amount_math_six_decimals():
    req = x402_gate.build_requirements()
    # 5 USDT at 6 decimals -> "5000000" base units, digits-only string
    assert req["maxAmountRequired"] == "5000000"
    assert req["maxAmountRequired"].isdigit()
    assert int(req["maxAmountRequired"]) == 5 * 10 ** 6
    assert (int(req["maxAmountRequired"])
            / 10 ** req["extra"]["decimals"]) == 5.0


def test_x402_mock_round_trip():
    req = x402_gate.build_requirements()
    payer = "0x" + "ab" * 20
    payload = x402_gate.build_mock_payload(req, payer=payer)

    verdict = facilitator.verify(payload, req, mode="mock")
    assert verdict["isValid"] and verdict["payer"] == payer

    receipt = facilitator.settle(payload, req, mode="mock")
    assert receipt["success"] is True
    assert receipt["status"] == "success"
    assert receipt["network"] == "eip155:196"
    assert receipt["payer"] == payer
    tx = receipt["transaction"]
    assert tx.startswith("0x") and len(tx) == 66, f"bad tx hash {tx!r}"
    assert all(c in "0123456789abcdef" for c in tx[2:])
    # deterministic: same payload -> same transaction hash
    assert facilitator.settle(payload, req, mode="mock")["transaction"] == tx

    # end to end through the gate, paying the v2 way: the PaymentPayload is
    # replayed in a PAYMENT-SIGNATURE header -> allowed + a receipt
    v = x402_gate.check({"PAYMENT-SIGNATURE": x402_gate.b64_json(payload)},
                        mode="mock")
    assert v.allowed, "valid PAYMENT-SIGNATURE payment must pass the gate"
    got = x402_gate.unb64_json(v.headers["PAYMENT-RESPONSE"])
    assert got == receipt, "PAYMENT-RESPONSE must be the settle() receipt"


def test_x402_legacy_x_payment_header_still_accepted():
    """The v1 X-PAYMENT header keeps working (PAYMENT-SIGNATURE wins if both
    are present)."""
    req = x402_gate.build_requirements()
    payload = x402_gate.build_mock_payload(req)
    wire = x402_gate.b64_json(payload)

    v = x402_gate.check({"X-PAYMENT": wire}, mode="mock")
    assert v.allowed, "legacy X-PAYMENT payment must pass the gate"
    assert "PAYMENT-RESPONSE" in v.headers

    # precedence: a bad PAYMENT-SIGNATURE is NOT rescued by a good X-PAYMENT
    v = x402_gate.check({"PAYMENT-SIGNATURE": "!!!not-base64!!!",
                         "X-PAYMENT": wire}, mode="mock")
    assert not v.allowed and v.status == 402, \
        "PAYMENT-SIGNATURE must take precedence over X-PAYMENT"


def test_x402_bad_payments_rejected():
    req = x402_gate.build_requirements()

    def rejected(header):
        v = x402_gate.check(header, mode="mock")
        assert not v.allowed and v.status == 402
        assert v.body.get("error")
        assert "PAYMENT-REQUIRED" in v.headers   # fresh challenge every time
        return v.body["error"]

    assert "base64" in rejected("!!!not-base64!!!").lower()

    wrong_net = x402_gate.build_mock_payload(req)
    wrong_net["network"] = "eip155:1"
    assert "network" in rejected(x402_gate.b64_json(wrong_net))

    wrong_scheme = x402_gate.build_mock_payload(req)
    wrong_scheme["scheme"] = "upto"
    assert "scheme" in rejected(x402_gate.b64_json(wrong_scheme))

    underpaid = x402_gate.build_mock_payload(req)
    underpaid["payload"]["authorization"]["value"] = "4999999"  # < 5 USDT
    assert "insufficient" in rejected(x402_gate.b64_json(underpaid))

    # real mode without OKX creds must fail fast with a clear message
    saved = {k: os.environ.pop(k, None)
             for k in ("OKX_X402_API_KEY", "OKX_X402_SECRET",
                       "OKX_X402_PASSPHRASE")}
    try:
        try:
            facilitator.verify(x402_gate.build_mock_payload(req), req,
                               mode="real")
            assert False, "real mode without creds should raise"
        except RuntimeError as e:
            assert "OKX_X402_API_KEY" in str(e)
        # through the gate, real-mode-without-creds surfaces as a clean 402
        v = x402_gate.check(
            x402_gate.b64_json(x402_gate.build_mock_payload(req)),
            mode="real")
        assert not v.allowed and v.status == 402
        assert "OKX_X402_API_KEY" in v.body["error"]
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v


# --------------------------------------------------------------------------
def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
