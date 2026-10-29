"""Multi-part review receipts (review-multi-v1; tiers review-3x2 and review-3x3), checked against the server's own
signed test vectors: tests/multi-review-vectors.json, copied from agent-referee-server (scripts/gen-multi-vectors.mjs).
The JS client's test/multi-review.test.mjs runs the same vectors."""

import json
import os
import re

import pytest

from agent_referee_client import AgentReferee, RefereeError, verify_review_outcome, verify_signature
from agent_referee_client._canonical import canonical_json, sha256_hex, utf16_len

HERE = os.path.dirname(__file__)
VX = json.load(open(os.path.join(HERE, "multi-review-vectors.json"), encoding="utf-8"))
KEYS = [VX["public_key"]]
CASES = {c["id"]: c for c in VX["cases"]}


def check(c, **opts):
    opts = opts or {"task": c["given"]["task"], "pieces": c["given"]["pieces"]}
    signed = verify_signature(c["receipt"], KEYS)
    r = verify_review_outcome(c["receipt"], **opts)
    return signed and r["ok"], signed, r["problems"]


def test_building_blocks_match_the_server():
    for b in VX["building_blocks"]:
        recs = [{"index": i + 1, "name": p.get("name", ""), "sha256": sha256_hex(p["text"]), "chars": utf16_len(p["text"])} for i, p in enumerate(b["pieces"])]
        assert recs == b["records"]
        assert canonical_json(recs) == b["canonical"]
        assert sha256_hex(b["canonical"]) == b["pieces_sha256"]


@pytest.mark.parametrize("cid", list(CASES))
def test_every_vector_gives_the_expected_result(cid):
    c = CASES[cid]
    ok, signed, problems = check(c)
    assert ok == c["expect"]["ok"], (cid, problems)
    if c["expect"]["ok"]:
        assert problems == []
        assert c["receipt"]["payload"]["result"]["verdict"] == c["expect"]["verdict"]
    elif cid == "bad-signature":
        assert not signed
    else:
        assert problems


WANT = {
    "wrong-piece-text": r"Piece 2 is different",
    "missing-name": r"Piece 1's name differs",
    "pieces-swapped": r"in a different order",
    "wrong-task": r"different task",
    "wrong-count": r"This review has 2 pieces; you passed 3",
    "bad-pieces-sha256": r"pieces_sha256 doesn't match",
    "chars-as-code-points": r"Piece 1's length",
    "bad-verdict": r"the verdict should be",
    "two-votes": r"Expected 3 reviewer votes",
    "same-company": r"three different AI companies",
    "no-issues-dropped": r"issues_dropped",
    "has-output-sha256": r"never has output_sha256",
    "unknown-format": r"can't check, so nothing was confirmed\. Update the client",
    "old-tier-name": r"must be for a multi-part tier",
}


@pytest.mark.parametrize("cid", list(WANT))
def test_each_failing_vector_fails_for_its_own_reason(cid):
    _, _, problems = check(CASES[cid])
    assert any(re.search(WANT[cid], m) for m in problems), problems


def test_the_messages_match_the_js_client_word_for_word():
    # The same receipts give the same problem text in both clients (spot checks; the JS suite pins the same strings).
    _, _, p = check(CASES["pieces-swapped"])
    assert p == ["These are the review's pieces, but in a different order."]
    _, _, p = check(CASES["old-tier-name"])
    assert p == ["A review-multi-v1 receipt must be for a multi-part tier (review-3x2 or review-3x3), not review-3."]


def test_output_instead_of_pieces_and_no_pieces():
    c = CASES["valid-3x2"]
    r = verify_review_outcome(c["receipt"], task=c["given"]["task"], output="anything")
    assert not r["ok"] and "This is a multi-part review: check it with pieces, not output." in r["problems"]
    assert verify_review_outcome(c["receipt"], task=c["given"]["task"])["ok"]  # hashes only, like a check without output
    r = verify_review_outcome(c["receipt"], task=c["given"]["task"], pieces=c["given"]["pieces"], tier="review-3x3")
    assert "This review is review-3x2, not review-3x3." in r["problems"]


def test_multi_tier_without_format_and_pieces_on_a_single_review():
    c = CASES["valid-3x2"]
    payload = {k: v for k, v in c["receipt"]["payload"].items() if k != "receipt_format"}
    r = verify_review_outcome(payload, task=c["given"]["task"], pieces=c["given"]["pieces"])
    assert r["problems"] == ["A review-3x2 receipt must say receipt_format review-multi-v1; this one doesn't, so it can't be checked."]
    single = dict(c["receipt"]["payload"], tier="review-3")
    single.pop("receipt_format")
    r = verify_review_outcome(single, pieces=c["given"]["pieces"])
    assert "This is a single-piece review: check it with output, not pieces." in r["problems"]


def test_review_sends_pieces_never_output_and_checks_with_them(monkeypatch):
    c = CASES["valid-3x2"]
    sent = []

    def fake_call(self, method, path, body=None, *, pay=False, recoverable=False):
        sent.append((method, path, body, pay))
        p = c["receipt"]["payload"]
        return {"review_id": "x", "tier": "review-3x2", "result": p["result"], "votes": p["votes"], "receipt": c["receipt"]}

    monkeypatch.setattr(AgentReferee, "_call", fake_call)

    class Payer:
        wallet = c["receipt"]["payload"]["payer"]

    ref = AgentReferee("https://agentreferee.ai", public_keys=KEYS, payer=Payer(), proof_store=False)
    r = ref.review(c["given"]["task"], tier="review-3x2", pieces=c["given"]["pieces"], title="Handoff v5")
    assert r["check"]["ok"], r["check"]["problems"]
    method, path, body, pay = sent[0]
    assert (method, path, pay) == ("POST", "/review-3x2", True)
    assert body["pieces"] == c["given"]["pieces"] and "output" not in body
    for bad in [
        dict(task=c["given"]["task"], output="x", tier="review-3x2"),
        dict(task=c["given"]["task"], tier="review-3x2", pieces=[{"text": "only one"}]),
        dict(task=c["given"]["task"], tier="review-3x3", pieces=c["given"]["pieces"]),
        dict(task=c["given"]["task"], output="x", tier="review-3", pieces=c["given"]["pieces"]),
        dict(task=c["given"]["task"], tier="review-3x2", pieces=c["given"]["pieces"], free=True),
    ]:
        with pytest.raises(RefereeError, match="Nothing was sent"):
            ref.review(**bad)
    assert len(sent) == 1


def test_odd_receipts_are_refused_without_crashing():
    c = CASES["valid-3x2"]
    p = c["receipt"]["payload"]
    r = verify_review_outcome(dict(p, pieces=[p["pieces"][0], None]), task=c["given"]["task"], pieces=c["given"]["pieces"])
    assert not r["ok"] and "Piece 2's record in the receipt is malformed." in r["problems"]
    for tier in ("constructor", "toString", "__proto__"):
        assert verify_review_outcome(dict(p, tier=tier))["problems"] == [
            f"A review-multi-v1 receipt must be for a multi-part tier (review-3x2 or review-3x3), not {tier}."]
    r = verify_review_outcome(dict(p, funding="free"), task=c["given"]["task"], pieces=c["given"]["pieces"])
    assert "Multi-part reviews are paid only, but this receipt says it was free." in r["problems"]
    r = verify_review_outcome(dict(p, result=dict(p["result"], unanimous=True)), task=c["given"]["task"], pieces=c["given"]["pieces"])
    assert "The receipt's unanimous flag doesn't match the votes." in r["problems"]
    assert verify_review_outcome(c["receipt"], task=c["given"]["task"], output=None, pieces=None)["ok"]  # None means not given
    # Whole numbers written as 31.0 (after some re-serialisation) count as whole numbers, as in JavaScript.
    floats = dict(p, pieces=[dict(x, chars=float(x["chars"])) for x in p["pieces"]], votes=[dict(v, issues_dropped=float(v["issues_dropped"])) for v in p["votes"]])
    r = verify_review_outcome(floats, task=c["given"]["task"], pieces=c["given"]["pieces"])
    assert not any("malformed" in x or "issues_dropped" in x for x in r["problems"]), r["problems"]
