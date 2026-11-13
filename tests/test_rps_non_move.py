"""A reveal that isn't rock, paper or scissors: the seat is named and the round isn't scored.

Before this, such a round went to the second party, so a receipt where y won with "lizard" checked clean. The service
refuses non-moves at reveal, so only a fault in the service could sign one (2.0's 0847, Nov 11). The cases are shared
with the JS client (agent-referee-client, test/fixtures/rps-non-move-cases.json), which gives the same answers.
"""
import hashlib
import json
import pathlib

from agent_referee_client import verify_outcome

CASES = json.loads((pathlib.Path(__file__).parent / "rps-non-move-cases.json").read_text(encoding="utf-8"))


def _fill(x):
    if isinstance(x, str) and x.startswith("@commit:"):
        return hashlib.sha256(x[8:].encode("utf-8")).hexdigest()
    if isinstance(x, list):
        return [_fill(v) for v in x]
    if isinstance(x, dict):
        return {k: _fill(v) for k, v in x.items()}
    return x


def test_rps_non_move_cases():
    assert len(CASES["cases"]) == 4
    for c in CASES["cases"]:
        assert verify_outcome(_fill(c["payload"]))["problems"] == c["expect"], c["name"]
