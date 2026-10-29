"""Multi-part judge receipts (judge-multi-v1; tiers judge-3x2 and judge-3x3), checked against the server's own signed
test vectors: tests/multi-judge-vectors.json, copied from agent-referee-server (scripts/gen-multi-judge-vectors.mjs).
The JS client's test/multi-judge.test.mjs runs the same vectors with the same messages."""

import json
import os
import re

import pytest

from agent_referee_client import AgentReferee, RefereeError, Seat, verify_outcome, verify_signature
from agent_referee_client._canonical import canonical_json, sha256_hex, utf16_len

HERE = os.path.dirname(__file__)
VX = json.load(open(os.path.join(HERE, "multi-judge-vectors.json"), encoding="utf-8"))
KEYS = [VX["public_key"]]
CASES = {c["id"]: c for c in VX["cases"]}


def check(c, **kw):
    kw = kw or {"pieces": c["given"]["pieces"], "acks": c["given"].get("acks")}
    signed = verify_signature(c["receipt"], KEYS) and all(verify_signature(a, KEYS) for a in (kw.get("acks") or []))
    r = verify_outcome(c["receipt"], **kw)
    return signed and r["ok"], signed, r["problems"]


def test_building_blocks_match_the_server():
    assert VX["building_blocks"]
    for b in VX["building_blocks"]:
        recs = [{"index": i + 1, "sha256": sha256_hex(t), "chars": utf16_len(t)} for i, t in enumerate(b["texts"])]
        assert recs == b["records"]
        assert canonical_json(recs) == b["canonical"]
        assert sha256_hex(b["canonical"]) == b["pieces_sha256"]


def test_there_are_twenty_vectors():
    assert len(CASES) == 20


@pytest.mark.parametrize("cid", list(CASES))
def test_every_vector_gives_the_expected_result(cid):
    c = CASES[cid]
    ok, signed, problems = check(c)
    assert ok == c["expect"]["ok"], (cid, problems)
    if c["expect"]["ok"]:
        assert problems == []
        assert c["receipt"]["payload"]["outcome"]["winner"] == c["expect"]["winner"]
    elif cid == "bad-signature":
        assert not signed
    else:
        assert problems


WANT = {
    "wrong-piece-text": r"alpha's piece 2 is different",
    "pieces-swapped": r"alpha's pieces, but in a different order",
    "wrong-count": r"alpha has 2 pieces on judge-3x2; you passed 3",
    "bad-pieces-sha256": r"beta's pieces_sha256 doesn't match",
    "chars-as-code-points": r"alpha's piece 1 length",
    "missing-side": r"must have both sides' pieces",
    "has-submission-sha256": r"never has proof\.submission_sha256",
    "bad-winner": r"decision should be",
    "bad-tally": r"tally doesn't match",
    "two-votes": r"Expected 3 votes",
    "same-company": r"three different AI companies",
    "wrong-format-version": r"agent-referee-judge-v5-multi",
    "unknown-format": r"can't check, so nothing was confirmed\. Update the client",
    "old-tier-name": r"must be for a multi-part judge tier",
    "ack-pieces-changed": r"alpha's submission was changed",
    "forfeit-after-ack": r"beta is listed as forfeiting after the service accepted its submission",
}


@pytest.mark.parametrize("cid", list(WANT))
def test_each_failing_vector_fails_for_its_own_reason(cid):
    _, _, problems = check(CASES[cid])
    assert any(re.search(WANT[cid], m) for m in problems), problems


def test_without_pieces_and_with_a_seat():
    c = CASES["valid-3x2"]
    assert verify_outcome(c["receipt"])["ok"]
    seat = Seat(AgentReferee("https://agentreferee.ai", public_keys=KEYS), c["receipt"]["payload"]["match_id"], "alpha", "tok", "judge-3x2")
    seat.pieces = list(c["given"]["pieces"]["alpha"])
    assert verify_outcome(c["receipt"], seat=seat)["ok"]
    seat.pieces.reverse()
    assert "These are alpha's pieces, but in a different order." in verify_outcome(c["receipt"], seat=seat)["problems"]
    assert "The receipt has no pieces for gamma." in verify_outcome(c["receipt"], pieces={"gamma": ["x", "y"]})["problems"]


def test_submit_sends_pieces_and_refuses_wrong_shapes(monkeypatch):
    sent = []

    def fake_call(self, method, path, body=None, *, pay=False, recoverable=False):
        sent.append((path, body))
        return {"ok": True}

    monkeypatch.setattr(AgentReferee, "_call", fake_call)
    monkeypatch.setattr(AgentReferee, "wait_for", lambda self, mid, fn: {"phase": "done"})
    ref = AgentReferee("https://agentreferee.ai", public_keys=KEYS)
    seat = Seat(ref, "m1", "alpha", "tok", "judge-3x2")
    seat.submit(["Plan A: $40 in all.\n", {"text": "Ships in 2 days."}])
    assert sent[0] == ("/matches/m1/submit", {"seat": "alpha", "seat_token": "tok", "pieces": [{"text": "Plan A: $40 in all.\n"}, {"text": "Ships in 2 days."}]})
    assert seat.pieces == ["Plan A: $40 in all.\n", "Ships in 2 days."]
    for bad in ["one string", ["only one"], ["a", "b", "c"], ["a", "   "], [{"name": "x"}, "b"]]:
        with pytest.raises(RefereeError, match="Nothing was sent"):
            seat.submit(bad)
    with pytest.raises(RefereeError, match="one position"):
        Seat(ref, "m2", "alpha", "tok", "judge-3").submit(["a", "b"])
    assert len(sent) == 1


FORFEIT_MSG = "In a forfeit, the side that submitted must be the winner, and only the other side may be listed as forfeiting."


@pytest.mark.parametrize("change", [{"winner": "beta"}, {"winner": None}, {"winner": "mallory"}, {"forfeited": ["alpha", "beta"]}])
def test_forfeit_winner_and_list_must_match_who_submitted(change):
    c = CASES["valid-forfeit"]
    p = dict(c["receipt"]["payload"], outcome=dict(c["receipt"]["payload"]["outcome"], **change))
    r = verify_outcome(p, pieces=c["given"]["pieces"])
    assert not r["ok"] and FORFEIT_MSG in r["problems"], r["problems"]


def test_seat_pieces_are_checked_alongside_passed_pieces_and_records_are_exact():
    c = CASES["wrong-piece-text"]
    seat = Seat(AgentReferee("https://agentreferee.ai", public_keys=KEYS), c["receipt"]["payload"]["match_id"], "alpha", "tok", "judge-3x2")
    seat.pieces = list(c["given"]["pieces"]["alpha"])
    for pieces in (None, {}, {"beta": c["given"]["pieces"]["beta"]}):
        r = verify_outcome(c["receipt"], seat=seat, pieces=pieces)
        assert "alpha's piece 2 is different from the one in the receipt." in r["problems"], r["problems"]
    import copy
    v = copy.deepcopy(CASES["valid-3x2"]["receipt"]["payload"])
    v["proof"]["pieces"]["alpha"][0]["name"] = "alice"
    v["proof"]["pieces_sha256"]["alpha"] = sha256_hex(canonical_json(v["proof"]["pieces"]["alpha"]))
    assert "alpha's piece 1 record in the receipt is malformed." in verify_outcome(v)["problems"]
    assert 'pieces must be by seat: { "seat": ["text 1", "text 2"] }.' in verify_outcome(CASES["valid-3x2"]["receipt"], pieces=[["a", "b"]])["problems"]


def test_submit_counts_whitespace_like_the_server():
    from agent_referee_client.client import _js_trim
    assert _js_trim("\x1c") == "\x1c"  # the server (JavaScript trim) accepts this as text
    assert _js_trim("﻿ 　") == ""  # and refuses this as only spaces
