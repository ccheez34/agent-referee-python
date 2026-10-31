"""Judge spec §9: judge receipts carry the service's own advisory sentence and ``human_review: "required"`` inside the
signed part. The client accepts them, shows the receipt's own text, and passes the server's new answers on. The JS
client's test/advisory.test.mjs checks the same with the same vectors."""

import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from agent_referee_client import AgentReferee, RefereeError, judge_advisory, verify_outcome, verify_signature

HERE = os.path.dirname(__file__)
VX = json.load(open(os.path.join(HERE, "multi-judge-vectors.json"), encoding="utf-8"))
VALID = [c for c in VX["cases"] if c["expect"]["ok"]]


def test_every_vector_carries_both_fields_and_the_valid_ones_verify():
    for c in VX["cases"]:
        assert isinstance(c["receipt"]["payload"]["advisory"], str), c["id"]
        assert c["receipt"]["payload"]["human_review"] == "required", c["id"]
    assert len(VALID) >= 3
    for c in VALID:
        assert verify_signature(c["receipt"], [VX["public_key"]]), c["id"]


def test_no_claude_judge_sits_on_the_regenerated_panels():
    for c in VX["cases"]:
        p = c["receipt"]["payload"]
        text = json.dumps(p.get("proof", {}).get("judge", {})) + json.dumps(p.get("outcome", {}).get("votes", []))
        assert not re.search(r"claude|anthropic", text, re.I), c["id"]


def test_verify_outcome_shows_the_receipts_own_advisory_text():
    for c in VALID:
        r = verify_outcome(c["receipt"], pieces=c["given"]["pieces"], acks=c["given"].get("acks"))
        assert r["ok"], (c["id"], r["problems"])
        assert r["advisory"] == c["receipt"]["payload"]["advisory"]
        assert r["human_review"] == "required"
        assert c["receipt"]["payload"]["advisory"] in r["trusts"]
        assert any("person's review" in t for t in r["trusts"])


def test_judge_advisory_reads_a_receipt_a_payload_or_a_status():
    rc = VALID[0]["receipt"]
    status = {"phase": "done", "advisory": rc["payload"]["advisory"], "human_review": "required"}
    for doc in (rc, rc["payload"], status):
        a = judge_advisory(doc)
        assert a["advisory"] == rc["payload"]["advisory"]
        assert a["human_review_required"] is True
    empty = {"advisory": None, "human_review": None, "human_review_required": False}
    assert judge_advisory({"payload": {"tier": "rps"}}) == empty
    assert judge_advisory(None) == empty
    assert judge_advisory("not a receipt") == empty


def test_an_older_receipt_without_the_fields_gets_no_advisory_line():
    c = VALID[0]
    older = {k: v for k, v in c["receipt"]["payload"].items() if k not in ("advisory", "human_review")}
    r = verify_outcome(older, pieces=c["given"]["pieces"])
    assert r["advisory"] is None and r["human_review"] is None
    assert not any("person's review" in t for t in r["trusts"])


@pytest.mark.parametrize("status,error", [
    (400, "Claude models aren't offered as judges for now. Name models from other companies; GET /judge lists them."),
    (503, "judge-3 is closed for now: judge-3 needs a third AI company; agents can name one themselves (GET /judge)"),
])
def test_the_servers_own_text_for_the_new_answers_reaches_the_caller(status, error):
    class H(BaseHTTPRequestHandler):
        def _answer(self):
            body = json.dumps({"error": error}).encode()
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_POST = _answer

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ref = AgentReferee(url=f"http://127.0.0.1:{srv.server_address[1]}")
        with pytest.raises(RefereeError) as e:
            ref.create(tier="judge-3", seats=["a", "b"], rules="r")
        assert e.value.status == status
        assert str(e.value) == error
    finally:
        srv.shutdown()
