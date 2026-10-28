"""Cross-language checks: tests/vectors.json was produced by the SERVER's own code (logic.ts, review.ts, receipt.ts,
match.ts) and the JavaScript client 0.8.0 (tests/js/gen_vectors.mjs). Python must give exactly the same answers:
the same bytes, the same hashes, and for every receipt (genuine or tampered) the same ok, problems and trusts."""

import json
import pathlib
from types import SimpleNamespace

import pytest

from agent_referee_client import (
    canonical_json,
    combined_random,
    commitment,
    draw_pick,
    find_contradictions,
    sha256_hex,
    utf16_len,
    verify_outcome,
    verify_review_outcome,
    verify_series,
    verify_signature,
    weighted_pick,
)

V = json.loads((pathlib.Path(__file__).parent / "vectors.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", V["canonical"], ids=lambda c: c["json"][:30])
def test_canonical_json_matches_the_server(case):
    value = json.loads(case["json"])
    assert canonical_json(value) == case["canonical"]
    assert sha256_hex(canonical_json(value)) == case["sha256"]


@pytest.mark.parametrize("case", V["strings"], ids=lambda c: repr(c["text"][:20]))
def test_sha256_and_utf16_length_match(case):
    assert sha256_hex(case["text"]) == case["sha256"]
    assert utf16_len(case["text"]) == case["length"]


@pytest.mark.parametrize("case", V["randomness"], ids=lambda c: c["match_id"])
def test_randomness_matches_the_server(case):
    c = combined_random(case["match_id"], case["round"], case["seeds"])
    assert c["hex"] == case["combined_hex"]
    assert c["value"] == int(case["combined_value"])
    assert weighted_pick(case["weights"], c["value"]) == case["weighted_pick"]
    assert draw_pick(case["options"], c["value"]) == case["draw_pick"]
    assert commitment(case["commit_value"], case["nonce"]) == case["commitment"]


def _seat(s):
    if s is None:
        return None
    return SimpleNamespace(match_id=s["matchId"], seat=s["seat"], mine=s["mine"], acks=s["acks"], requested=s.get("requested"))


def _same_result(py, js):
    assert py["ok"] == js["ok"]
    assert py["problems"] == js["problems"]
    assert py["trusts"] == js["trusts"]
    if "recomputed" in js:
        assert py["recomputed"] == js["recomputed"]
    if "evidence" in js:
        assert [e["reason"] for e in py["evidence"]] == [e["reason"] for e in js["evidence"]]


@pytest.mark.parametrize("case", V["matches"], ids=lambda c: c["name"])
def test_match_receipts_check_out_exactly_like_js(case):
    receipt = case["receipt"]
    assert verify_signature(receipt, [V["public_key"]]) == case["signed"]
    _same_result(verify_outcome(receipt), case["without_seat"])
    if case["seat"] is not None:
        seat = _seat(case["seat"])
        _same_result(verify_outcome(receipt, seat=seat), case["with_seat"])
        assert [e["reason"] for e in find_contradictions(receipt, seat.acks)] == case["contradictions"]
        for ack in seat.acks:  # every step acknowledgment is signed by the same key
            assert verify_signature(ack, [V["public_key"]])


@pytest.mark.parametrize("case", V["series"], ids=lambda c: c["name"])
def test_series_check_like_js(case):
    py = verify_series(case["receipts"])
    assert py["ok"] == case["expected"]["ok"]
    assert py["problems"] == case["expected"]["problems"]
    assert canonical_json(py["tally"]) == canonical_json(case["expected"]["tally"])


@pytest.mark.parametrize("case", V["reviews"], ids=lambda c: c["name"])
def test_review_receipts_check_out_exactly_like_js(case):
    o = case["opts"]
    assert verify_signature(case["receipt"], [V["review_public_key"]]) == case["signed"]
    py = verify_review_outcome(case["receipt"], task=o.get("task"), output=o.get("output"), tier=o.get("tier"), panel=o.get("panel"))
    _same_result(py, case["expected"])


def test_free_receipts_check_out_like_paid_ones_and_the_funding_check_works():
    free = [c for c in V["reviews"] if c.get("funding") == "free"]
    paid = [c for c in V["reviews"] if c["name"] == "review single ok"]
    assert free and paid
    for c in free:
        o = c["opts"]
        assert c["receipt"]["payload"]["funding"] == "free" and c["receipt"]["payload"]["payer"] is None
        r = verify_review_outcome(c["receipt"], task=o["task"], output=o["output"], tier=o["tier"], panel=o.get("panel"), funding="free")
        assert r["ok"], r["problems"]
    o = paid[0]["opts"]
    r = verify_review_outcome(paid[0]["receipt"], task=o["task"], output=o["output"], funding="free")
    assert not r["ok"] and r["problems"] == ['A free review\'s receipt should say "funding": "free".', "A free review's receipt shouldn't name a payer."]
