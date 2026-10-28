"""Receipts signed by the service's own code (tests/fixture_review.json) must verify in Python."""
import copy
import json
import pathlib
import unittest

from agent_referee_client import canonical_json, sha256_hex, verify_review_outcome, verify_signature

FIX = json.loads((pathlib.Path(__file__).parent / "fixture_review.json").read_text(encoding="utf-8"))
KEY, INP, REC = FIX["public_key"], FIX["input"], FIX["receipt"]


class VerifyTests(unittest.TestCase):
    def test_signature_from_the_service_code_verifies(self):
        self.assertTrue(verify_signature(REC, [KEY]))

    def test_wrong_key_or_tampered_payload_fails(self):
        self.assertFalse(verify_signature(REC, ["AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="]))
        bad = copy.deepcopy(REC)
        bad["payload"]["result"]["verdict"] = "fail"
        self.assertFalse(verify_signature(bad, [KEY]))

    def test_review_outcome_recomputes(self):
        r = verify_review_outcome(REC, task=INP["task"], output=INP["output"], tier="review-3")
        self.assertTrue(r["ok"], r["problems"])
        self.assertEqual(REC["payload"]["result"]["verdict"], "pass")

    def test_different_output_task_or_tier_is_caught(self):
        self.assertFalse(verify_review_outcome(REC, output="hello")["ok"])
        self.assertFalse(verify_review_outcome(REC, task="something else")["ok"])
        self.assertFalse(verify_review_outcome(REC, tier="review")["ok"])

    def test_tampered_verdict_or_tally_is_caught(self):
        bad = copy.deepcopy(REC)
        bad["payload"]["result"]["verdict"] = "split"
        self.assertIn("should be pass", " ".join(verify_review_outcome(bad)["problems"]))
        bad = copy.deepcopy(REC)
        bad["payload"]["votes"][0]["total"] = 1
        self.assertFalse(verify_review_outcome(bad)["ok"])

    def test_not_a_review(self):
        self.assertFalse(verify_review_outcome({"payload": {"tier": "weighted", "outcome": {}}})["ok"])

    def test_helpers(self):
        self.assertEqual(canonical_json({"b": 1, "a": [True, None, "x"]}), '{"a":[true,null,"x"],"b":1}')
        self.assertEqual(sha256_hex(INP["output"]), REC["payload"]["output_sha256"])


if __name__ == "__main__":
    unittest.main()
