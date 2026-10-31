"""Terms acceptance, option A of the terms-binding spec: the owner's accepted version travels as x-accept-terms, and
receipts that record it can be read back. Off unless accept_terms is set."""
import json

import pytest

from agent_referee_client import AgentReferee, RefereeError, is_terms_refusal, receipt_terms


def recorder(status=200, body=None):
    seen = []

    def transport(method, url, headers, data, timeout):
        seen.append(dict(headers))
        return status, {"content-type": "application/json"}, json.dumps(body or {}).encode()

    return seen, transport


def test_unset_sends_no_header():
    seen, t = recorder()
    AgentReferee("https://agentreferee.ai", transport=t)._call("GET", "/")
    assert "x-accept-terms" not in seen[0]


def test_set_sends_it_on_every_request_and_bad_versions_are_refused():
    seen, t = recorder()
    ref = AgentReferee("https://agentreferee.ai", transport=t, accept_terms="v2026-11-01")
    ref._call("GET", "/")
    ref._call("POST", "/review", {"task": "x" * 20, "output": "y"})
    assert [s.get("x-accept-terms") for s in seen] == ["v2026-11-01", "v2026-11-01"]
    for bad in ["2026-11-01", "v26-11-01", "latest", "v2026-11-01; drop"]:
        with pytest.raises(ValueError):
            AgentReferee(accept_terms=bad)


def test_a_terms_refusal_is_recognised_and_passes_through():
    msg = "send x-accept-terms: v2026-11-01 to accept https://agentreferee.ai/terms; nothing was charged"
    _, t = recorder(428, {"error": msg})
    with pytest.raises(RefereeError) as e:
        AgentReferee("https://agentreferee.ai", transport=t)._call("GET", "/")
    assert is_terms_refusal(e.value)
    assert "nothing was charged" in str(e.value)
    assert not is_terms_refusal(RefereeError("busy", 503, None))
    assert not is_terms_refusal(ValueError(msg))


def test_receipt_terms_reads_the_signed_version():
    sha = "a" * 64
    assert receipt_terms({"payload": {"terms_version": "v2026-11-01", "terms_sha256": sha}}) == {"version": "v2026-11-01", "sha256": sha, "matches": None}
    assert receipt_terms({"payload": {"terms_version": "v2026-11-01"}}, "v2026-11-01")["matches"] is True
    assert receipt_terms({"payload": {"terms_version": "v2026-11-01", "terms_sha256": "x"}}, "v2026-12-01") == {"version": "v2026-11-01", "sha256": None, "matches": False}
    assert receipt_terms({"payload": {}}) is None
    assert receipt_terms(None) is None
