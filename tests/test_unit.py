"""Client behaviour with a scripted transport: no network, no Node."""

import base64
import json
import os

import pytest

from agent_referee_client import (
    AgentReferee,
    FileProofStore,
    MemoryProofStore,
    RefereeError,
    X402Payer,
    canonical_json,
    read_wallet_key,
    verify_signature,
)
from agent_referee_client.client import _clean


class Script:
    """A transport that answers from a list of (method, path-prefix, status, body, headers) rules, in order."""

    def __init__(self, *rules):
        self.rules = list(rules)
        self.sent = []

    def __call__(self, method, url, headers, body, timeout):
        path = url.split("://", 1)[1].split("/", 1)[1]
        self.sent.append((method, "/" + path, headers, json.loads(body) if body else None))
        for i, (m, prefix, status, data, rh) in enumerate(self.rules):
            if m == method and ("/" + path).startswith(prefix):
                if callable(status):
                    status, data, rh = status(headers, json.loads(body) if body else None)
                raw = data.encode() if isinstance(data, str) else json.dumps(data).encode()
                return status, rh or {}, raw
        raise AssertionError(f"unexpected {method} /{path}")


STATUS = {"match_id": "a" * 24, "tier": "weighted", "phase": "joining", "seats": [{"seat": "alpha"}, {"seat": "beta"}], "weights": {"alpha": 1, "beta": 1}}


def test_join_retries_on_408_then_reports_no_charge():
    t = Script(("GET", "/matches/", 200, STATUS, None), ("POST", "/join/weighted", 408, {"error": "Timed out waiting for the other seats."}, None))
    ref = AgentReferee("https://agentreferee.ai", transport=t, join_retries=2)
    with pytest.raises(RefereeError) as e:
        ref.join("a" * 24, "alpha")
    assert e.value.status == 408 and "You were not charged" in e.value.message
    assert sum(1 for m, p, *_ in t.sent if p == "/join/weighted") == 3


def test_invite_match_guest_seat_must_use_the_invite():
    t = Script(("GET", "/matches/", 200, {**STATUS, "host_pays": "alpha"}, None))
    with pytest.raises(RefereeError) as e:
        AgentReferee(transport=t).join("a" * 24, "beta")
    assert "invite match" in e.value.message


def test_invite_links_from_either_official_address_and_no_other():
    ref = AgentReferee("https://agentreferee.ai", transport=Script())
    mid = "0123456789abcdef01234567"
    assert ref._parse_invite(f"https://agent-referee.fairplayclub.workers.dev/invite/{mid}?token=t0k") == (mid, "t0k")
    assert ref._parse_invite({"match_id": mid, "invite_token": "x"}) == (mid, "x")
    with pytest.raises(RefereeError, match="different service"):
        ref._parse_invite(f"https://evil.example/invite/{mid}?token=t")
    with pytest.raises(RefereeError, match="doesn't look like"):
        ref._parse_invite("https://agentreferee.ai/invite/xyz?token=t")
    with pytest.raises(RefereeError, match="must be the link"):
        ref._parse_invite("not a link")


def test_errors_keep_the_service_wording_and_retry_after():
    t = Script(("POST", "/free/review", 503, {"error": "busy: the AI reviewers are at capacity. Your free review wasn't used.", "busy": True}, {"retry-after": "12"}))
    with pytest.raises(RefereeError) as e:
        AgentReferee(transport=t).review("Translate hola into English.", "hello", free=True)
    assert e.value.message == "busy: the AI reviewers are at capacity. Your free review wasn't used." and e.value.retry_after == 12


def test_free_review_must_come_back_funding_free():
    receipt = {"payload": {"type": "review", "tier": "review"}, "signature": "x"}
    t = Script(("POST", "/free/review", 200, {"result": {"verdict": "pass"}, "receipt": receipt}, None))
    r = AgentReferee(transport=t, public_keys=["AAAA"]).review("Translate hola into English.", "hello", free=True)
    assert not r["check"]["ok"]
    assert 'A free review\'s receipt should say "funding": "free".' in r["check"]["problems"]


def test_free_review_never_sends_a_payment_even_with_a_payer():
    class Payer:
        wallet = "0x1"

        def payment_header(self, *a):
            raise AssertionError("a free review must never pay")

    t = Script(("POST", "/free/review", 402, {"x402Version": 1, "accepts": []}, None))
    with pytest.raises(RefereeError) as e:
        AgentReferee(transport=t, payer=Payer(), proof_store=False).review("Translate hola into English.", "hello", free=True)
    assert e.value.status == 402 and all("X-PAYMENT" not in h for _, _, h, _ in t.sent)


def test_review_has_no_demo_mode_and_checks_the_tier():
    with pytest.raises(RefereeError, match="no demo mode"):
        AgentReferee(demo=True, transport=Script()).review("Translate hola into English.", "hello")
    with pytest.raises(RefereeError, match="tier must be"):
        AgentReferee(transport=Script()).review("Translate hola into English.", "hello", tier="review-5")


def test_a_paid_review_that_gets_no_answer_says_what_to_do():
    def boom(*a):
        raise ConnectionResetError("reset")

    with pytest.raises(RefereeError) as e:
        AgentReferee(transport=boom, payer=object(), proof_store=False).review("Translate hola into English.", "hello")
    assert e.value.status == 0 and "basescan.org" in e.value.message and "hello@agentreferee.ai" in e.value.message


def test_none_values_are_never_sent():
    assert _clean({"a": None, "b": {"c": None, "d": 1}, "e": [None, 2]}) == {"b": {"d": 1}, "e": [None, 2]}
    t = Script(("POST", "/matches", 200, {"match_id": "b" * 24}, None))
    AgentReferee(transport=t, demo=True).create(tier="weighted", seats=["a", "b"], weights={"a": 1, "b": 1}, title=None)
    assert t.sent[0][3] == {"tier": "weighted", "seats": ["a", "b"], "weights": {"a": 1, "b": 1}, "demo": True}


def test_proof_stores(tmp_path):
    for store in (FileProofStore(str(tmp_path / "p")), MemoryProofStore()):
        store.save("u|b", "PROOF", {"match_id": "m", "seat": "alpha"})
        [e] = store.list()
        assert e["proof"] == "PROOF" and e["match_id"] == "m" and e["key"] == "u|b"
        store.remove("u|b")
        store.remove("u|b")  # removing twice is fine
        assert store.list() == []
    if os.name == "posix":
        s = FileProofStore(str(tmp_path / "q"))
        s.save("k", "P")
        [f] = os.listdir(tmp_path / "q")
        assert (os.stat(tmp_path / "q" / f).st_mode & 0o777) == 0o600


def test_recover_pending_clears_proofs_that_never_settled():
    store = MemoryProofStore()
    store.save("k1", "P" * 30, {"match_id": "c" * 24, "seat": "alpha"})
    store.save("k2", "P" * 30, {})  # no match id: just cleaned up
    t = Script(("POST", "/matches/", 404, {"error": "No seat was claimed with that payment."}, None))
    assert AgentReferee(transport=t).recover_pending(store, min_age_s=0) == [] and store.list() == []


def test_read_wallet_key(tmp_path):
    k = "0x" + "ab" * 32
    f = tmp_path / "w.txt"
    f.write_text(f"address: 0x28275fA8FE39996501e160104A23E1aF5E005F58\nkey: {k}\n")
    assert read_wallet_key(str(f)) == k
    f.write_text(f"tx {'0x' + 'cd' * 32}\nprivate key {k}\n")
    assert read_wallet_key(str(f)) == k
    f.write_text("nothing here")
    with pytest.raises(ValueError):
        read_wallet_key(str(f))


def test_payer_needs_an_explicit_cap():
    for bad in (0, -1, 1.5, True, None):
        with pytest.raises(ValueError):
            X402Payer("0x" + "11" * 32, bad)


def test_signature_base64_like_atob():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    sk = Ed25519PrivateKey.generate()
    pk = base64.b64encode(sk.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    payload = {"type": "review", "x": "😀"}
    sig = base64.b64encode(sk.sign(canonical_json(payload).encode())).decode()
    assert verify_signature({"payload": payload, "signature": sig}, [pk])
    assert verify_signature({"payload": payload, "signature": sig.rstrip("=")}, [pk])  # atob accepts no padding
    assert not verify_signature({"payload": payload, "signature": sig[:-4] + "!!!!"}, [pk])
    assert not verify_signature({"payload": payload, "signature": sig, "public_key": "other"}, [pk])
    assert not verify_signature({"payload": {}, "signature": sig}, [pk])
    assert not verify_signature({"payload": payload}, [pk])


def test_canonical_json_refuses_what_json_cant_hold():
    with pytest.raises(TypeError):
        canonical_json({"a": object()})
    assert canonical_json({"nan": float("nan"), "inf": float("inf")}) == '{"inf":null,"nan":null}'


def test_payer_refuses_wrong_network_or_recipient(fake_payer):
    from agent_referee_client.pay import PaymentRefused

    body = {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base-sepolia", "maxAmountRequired": "10", "resource": "r", "payTo": "0xabc", "maxTimeoutSeconds": 60, "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}]}
    with pytest.raises(PaymentRefused, match="network"):
        fake_payer(100).payment_header(body, {})
    body["accepts"][0]["network"] = "base"
    with pytest.raises(PaymentRefused):
        fake_payer(100, pay_to="0xF6B53AaCf822A36b7B2aF9C45BabE6E367ede4d3").payment_header(body, {})
    h = fake_payer(100).payment_header(body, {})
    signed = json.loads(base64.b64decode(h))
    assert signed["x402Version"] == 1 and signed["network"] == "base" and signed["payload"]["authorization"]["value"] == "10"


def test_recover_pending_leaves_fresh_proofs_alone():
    store = MemoryProofStore()
    store.save("k", "P" * 30, {"match_id": "c" * 24, "seat": "alpha"})  # maybe another agent's join, still running
    t = Script(("POST", "/matches/", 404, {"error": "No seat."}, None))
    assert AgentReferee(transport=t).recover_pending(store) == []
    assert len(store.list()) == 1 and t.sent == []
    assert AgentReferee(transport=t).recover_pending(store, min_age_s=0) == [] and store.list() == []


def test_a_string_public_key_is_one_key():
    assert AgentReferee(public_keys="abc", transport=Script()).public_keys == ["abc"]


def test_paid_join_answered_without_a_token_recovers_with_the_proof(fake_payer):
    paid = {"n": 0}

    def join(headers, body):
        if "X-PAYMENT" not in headers:
            return 402, {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "15000", "resource": "r", "payTo": "0xF6B53AaCf822A36b7B2aF9C45BabE6E367ede4d3", "maxTimeoutSeconds": 60, "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}]}, None
        paid["n"] += 1
        return 200, "<html>gateway</html>", None

    t = Script(("GET", "/matches/", 200, STATUS, None), ("POST", "/join/weighted", join, None, None),
               ("POST", "/matches/" + "a" * 24 + "/recover", 200, {"seat": "alpha", "seat_token": "tok"}, None))
    s = AgentReferee(transport=t, payer=fake_payer(15_000), proof_store=MemoryProofStore()).join("a" * 24, "alpha")
    assert s.token == "tok" and paid["n"] == 1
    rec = [b for m, p, h, b in t.sent if p.endswith("/recover")]
    assert len(rec) == 1 and len(rec[0]["payment"]) > 20


def test_a_408_attempt_leaves_no_stale_proof_behind(fake_payer):
    calls = {"join": 0}

    def join(headers, body):
        if "X-PAYMENT" not in headers:
            calls["join"] += 1
            if calls["join"] == 2:
                return 503, {"error": "busy: judges at capacity. Nothing was charged.", "busy": True, "retry_after_s": 5}, None
            return 402, {"x402Version": 1, "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "15000", "resource": "r", "payTo": "0xF6B53AaCf822A36b7B2aF9C45BabE6E367ede4d3", "maxTimeoutSeconds": 60, "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}]}, None
        return 408, {"error": "Timed out waiting for the other seats. Nothing was charged."}, None

    t = Script(("GET", "/matches/", 200, STATUS, None), ("POST", "/join/weighted", join, None, None))
    with pytest.raises(RefereeError) as e:
        AgentReferee(transport=t, payer=fake_payer(15_000), proof_store=MemoryProofStore(), join_retries=3).join("a" * 24, "alpha")
    assert e.value.status == 503 and e.value.retry_after == 5  # the busy answer is reported, not a pointless recovery
    assert not any(p.endswith("/recover") for _, p, _, _ in t.sent)


def test_wallet_key_must_be_a_whole_64_hex_value(tmp_path):
    k = "0x" + "ab" * 32
    f = tmp_path / "w.txt"
    f.write_text(f"signature 0x{'cd' * 65}\nkey {k}\n")  # a 130-hex value isn't a key, even its first 64 digits
    assert read_wallet_key(str(f)) == k
    f.write_text(f"{k}\n{k.upper().replace('0X', '0x')}\n")  # the same key in two cases is still one key
    assert read_wallet_key(str(f)) == k


def test_settled_never_raises():
    from agent_referee_client.pay import X402Payer as P

    p = P.__new__(P)
    p.last_settlement = None
    assert p.settled(None) is None and p.settled({"X-Payment-Response": "!!notbase64"}) == {"raw": "!!notbase64"}
    assert p.settled({"x-payment-response": base64.b64encode(b"5").decode()}) == {"raw": "NQ=="}
    assert p.settled(42) is None


def test_offers_must_have_whole_number_string_amounts(fake_payer):
    from agent_referee_client.pay import PaymentRefused

    offer = {"scheme": "exact", "network": "base", "resource": "r", "payTo": "0xabc", "maxTimeoutSeconds": 60, "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
    for bad in (50000.9, "50000.9", None, "1e4"):
        with pytest.raises(PaymentRefused):
            fake_payer(100_000).payment_header({"x402Version": 1, "accepts": [{**offer, "maxAmountRequired": bad}]}, {})
    with pytest.raises(PaymentRefused):
        fake_payer(100_000).payment_header({"x402Version": 1, "accepts": 5}, {})


def test_origins_and_dict_invites():
    from agent_referee_client.client import _origin

    assert _origin("http://[::1]:8080/x") == "http://[::1]:8080"
    assert _origin("HTTPS://AgentReferee.ai:443/") == "https://agentreferee.ai"
    with pytest.raises(RefereeError, match="invalid port"):
        _origin("https://agentreferee.ai:99999/")
    ref = AgentReferee(transport=Script())
    with pytest.raises(RefereeError, match="24 lowercase hex"):
        ref._parse_invite({"match_id": "../../admin", "invite_token": "t"})


def test_a_join_answer_without_a_seat_token_is_an_error_not_a_dead_seat():
    t = Script(("GET", "/matches/", 200, {**STATUS, "demo": True}, None), ("POST", "/demo/join/weighted", 200, {"ok": True}, None))
    with pytest.raises(RefereeError, match="no seat token"):
        AgentReferee(transport=t, demo=True).join("a" * 24, "alpha")


def test_recover_pending_removes_unusable_entries_whatever_their_age():
    store = MemoryProofStore()
    store.save("k", "", {"seat": "alpha"})  # no match_id, no proof
    assert AgentReferee(transport=Script()).recover_pending(store) == [] and store.list() == []


def test_file_store_replaces_atomically(tmp_path):
    s = FileProofStore(str(tmp_path / "p"))
    s.save("k", "ONE", {})
    s.save("k", "TWO", {})
    assert [e["proof"] for e in s.list()] == ["TWO"] and not [n for n in os.listdir(tmp_path / "p") if n.endswith(".tmp")]


def test_huge_integers_become_null_like_javascript():
    assert canonical_json(10**400) == "null"


def test_no_hidden_one_dollar_cap_inside_the_payment_library(fake_payer):
    offer = {"scheme": "exact", "network": "base", "maxAmountRequired": "1500000", "resource": "r", "payTo": "0xabc", "maxTimeoutSeconds": 60, "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"}
    h = fake_payer(2_000_000).payment_header({"x402Version": 1, "accepts": [offer]}, {})
    assert json.loads(base64.b64decode(h))["payload"]["authorization"]["value"] == "1500000"


def test_the_default_transport_never_follows_a_redirect_so_a_payment_header_never_moves():
    import http.server
    import threading

    from agent_referee_client.client import _urllib_transport

    seen = []

    class Elsewhere(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    class Redirect(Elsewhere):
        def do_POST(self):
            # Read the body first: on Windows, closing a socket with unread data resets the connection (WinError 10053).
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{other.server_port}/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

    other = http.server.HTTPServer(("127.0.0.1", 0), Elsewhere)
    first = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    for s in (other, first):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        status, headers, _ = _urllib_transport("POST", f"http://127.0.0.1:{first.server_port}/review", {"X-PAYMENT": "proof"}, b"{}", 10)
        assert status == 302 and "location" in headers and seen == []
        ref = AgentReferee(f"http://127.0.0.1:{first.server_port}")
        with pytest.raises(RefereeError, match="never follows redirects") as e:
            ref._call("POST", "/review", {"task": "x"})
        assert e.value.status == 302 and seen == []
    finally:
        other.shutdown()
        first.shutdown()


# The multi-part tiers (spec draft 3) add receipt formats this version can't check. Until the client learns them,
# a receipt in either format must be refused with a clear message, never passed. The judge case is the dangerous one:
# a receipt that kept tier "judge-3" and only added receipt_format would pass the old checks, because the verdict
# recomputes from the votes. The JS client's test/format-refusal.test.mjs runs the same cases.
UPDATE = "Update the client (pip install -U agent-referee-client)."
FALSY_AND_ODD = [None, "", 0, False, "foo"]  # every one is refused, not just truthy strings


def format_msg(shown):
    return f"This receipt uses the {shown} format, which this version of agent-referee-client can't check, so nothing was confirmed. {UPDATE}"


def _shown(fmt):  # how both clients print a value (JavaScript's String())
    if fmt is None:
        return "null"
    if isinstance(fmt, bool):  # checked before int: in Python, False == 0
        return "true" if fmt else "false"
    return str(fmt)


def _fixtures():
    import json
    import pathlib

    here = pathlib.Path(__file__).parent
    fx = json.loads((here / "fixture_review.json").read_text("utf-8"))
    v = json.loads((here / "vectors.json").read_text("utf-8"))
    judge = next(m["receipt"]["payload"] for m in v["matches"] if m["receipt"]["payload"]["tier"] == "judge-3")
    return judge, fx["receipt"]["payload"], fx["input"]["task"], fx["input"]["output"]


def _review_multi(review):
    from agent_referee_client import canonical_json, sha256_hex

    r = {k: x for k, x in review.items() if k != "output_sha256"}
    pieces = [{"index": 1, "name": "", "sha256": sha256_hex("a"), "chars": 1}, {"index": 2, "name": "", "sha256": sha256_hex("b"), "chars": 1}]
    return dict(r, tier="review-3x2", receipt_format="review-multi-v1", pieces=pieces, pieces_sha256=sha256_hex(canonical_json(pieces)))


def test_control_todays_receipts_still_pass():
    from agent_referee_client import verify_outcome, verify_review_outcome, verify_series

    judge, review, task, output = _fixtures()
    assert verify_outcome(judge)["ok"] is True
    assert verify_review_outcome(review)["ok"] is True
    assert verify_review_outcome(review, task=task, output=output, tier="review-3")["ok"] is True
    assert verify_series([judge])["ok"] is True  # so the series refusals below aren't a check that refuses everything


def test_judge_receipts_with_an_unknown_format_are_refused_and_judge_multi_v1_only_on_its_tiers():
    # Since 0.3.0 judge-multi-v1 is checked (tests/test_multi_judge.py runs the server's vectors), but only on
    # judge-3x2 and judge-3x3: on judge-3 it would otherwise recompute from the votes and pass.
    from agent_referee_client import verify_outcome

    judge = _fixtures()[0]
    for fmt in ["judge-multi-v2", "review-multi-v1", *FALSY_AND_ODD]:
        assert verify_outcome(dict(judge, receipt_format=fmt))["problems"] == [format_msg(_shown(fmt))], fmt
        assert verify_outcome(dict(judge, receipt_format=fmt))["ok"] is False
    assert verify_outcome(dict(judge, receipt_format="judge-multi-v1"))["problems"] == [
        "A judge-multi-v1 receipt must be for a multi-part judge tier (judge-3x2 or judge-3x3), not judge-3."]
    assert verify_outcome(dict(judge, tier="judge-3x2"))["problems"] == [
        "A judge-3x2 receipt must say receipt_format judge-multi-v1; this one doesn't, so it can't be checked."]
    for tier in ("judge-9x9", ["judge-3"]):
        c = verify_outcome(dict(judge, tier=tier))
        assert c["ok"] is False and c["problems"] == [f'Unknown tier "{_shown(tier) if not isinstance(tier, list) else "judge-3"}".']


def test_review_receipts_with_an_unknown_format_are_refused_with_or_without_texts():
    from agent_referee_client import verify_review_outcome

    _, review, task, output = _fixtures()
    for r in [dict(review, receipt_format="judge-multi-v1")] + [dict(review, receipt_format=f) for f in FALSY_AND_ODD]:
        for kw in ({}, {"task": task, "output": output}):
            c = verify_review_outcome(r, **kw)
            assert c["ok"] is False and c["problems"] == [format_msg(_shown(r["receipt_format"]))], (r["receipt_format"], kw)


def test_review_multi_v1_is_checked_never_passed_on_a_shortcut():
    # Since 0.2.0 the client knows review-multi-v1 (tests/test_multi.py runs the server's vectors). A receipt built
    # from a single-piece one is still refused: its votes lack issues_dropped.
    from agent_referee_client import verify_outcome, verify_review_outcome

    judge, review, task, output = _fixtures()
    m = verify_review_outcome(_review_multi(review), task=task, pieces=[{"text": "a"}, {"text": "b"}])
    assert m["ok"] is False and any("issues_dropped" in x for x in m["problems"]), m["problems"]
    old = verify_review_outcome(dict(review, receipt_format="review-multi-v1"), task=task, output=output)
    assert old["problems"] == ["A review-multi-v1 receipt must be for a multi-part tier (review-3x2 or review-3x3), not review-3."]
    j = verify_outcome(dict(judge, receipt_format="review-multi-v1"))  # a review format on a match receipt
    assert j["problems"] == [format_msg("review-multi-v1")]


def test_unknown_review_tiers_are_refused_clearly():
    from agent_referee_client import verify_review_outcome

    review = _fixtures()[1]
    c = verify_review_outcome(dict(review, tier="review-9x9"))
    assert c["ok"] is False and c["problems"] == [f"This client doesn't know the review-9x9 review tier, so nothing was confirmed. {UPDATE}"]
    assert verify_review_outcome(dict(review, tier=["review-3"]))["ok"] is False  # an odd tier value is refused, not a crash


def test_series_with_an_unknown_format_receipt_anywhere_is_refused():
    from agent_referee_client import verify_series

    judge = _fixtures()[0]
    bad = lambda f: dict(judge, receipt_format=f)  # noqa: E731
    for series, fmt in (([bad("judge-multi-v9")], "judge-multi-v9"), ([judge, bad(None)], None), ([judge, judge, bad("foo")], "foo"), ([bad(""), judge], "")):
        c = verify_series(series)
        assert c["ok"] is False and c["problems"] == [format_msg(_shown(fmt))]
