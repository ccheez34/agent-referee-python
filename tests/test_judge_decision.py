"""Re-deriving a judge decision (_check_judge_decision and _criteria_pick, through verify_outcome) on receipts the
tests change on purpose. The JS client's test/judge-decision.test.mjs runs the same cases with the same messages. The
base is the server-made judge-3 payload in vectors.json (the JS client's fixtures/receipts.json holds the same one)."""

import copy
import json
import pathlib

from agent_referee_client import verify_outcome, verify_review_outcome

HERE = pathlib.Path(__file__).parent
_VX = json.loads((HERE / "vectors.json").read_text("utf-8"))
JUDGE_3 = next(m["receipt"]["payload"] for m in _VX["matches"] if m["receipt"]["payload"]["tier"] == "judge-3")
REVIEW_3 = json.loads((HERE / "fixture_review.json").read_text("utf-8"))["receipt"]["payload"]


def panel_receipt(parties, rule, picks, extra):
    """The judge-3 receipt with these parties, rule and picks (one per panelist, None for a mistrial vote)."""
    p = copy.deepcopy(JUDGE_3)
    p["parties"] = parties
    p["proof"]["judge"]["decision_rule"] = rule
    votes = [dict(v, winner=picks[i]) for i, v in enumerate(p["outcome"]["votes"])]
    p["outcome"] = dict({"decision_rule": rule, "votes": votes}, **extra)
    return p


def single_receipt(parties, outcome):
    """A one-judge receipt (tier "judge") with scores on agreed criteria."""
    return {"tier": "judge", "parties": parties, "outcome": outcome, "proof": {"judge": {"model": "openai/gpt-6-luna"}}}


def problems(p):
    return verify_outcome(p)["problems"]


def test_the_base_receipt_checks_clean():
    assert problems(JUDGE_3) == []


def test_unanimous_every_vote_must_pick_the_same_party():
    party = panel_receipt(["buyer", "seller"], "unanimous", ["buyer"] * 3, {"winner": "buyer", "tally": {"buyer": 3, "seller": 0, "mistrial": 0}})
    assert problems(party) == []
    # Found in the clean-work check (Nov 4): three votes for "zed", which is no party, made "zed" the winner.
    r = verify_outcome(panel_receipt(["buyer", "seller"], "unanimous", ["zed"] * 3, {"winner": "zed"}))
    assert r["ok"] is False
    assert r["problems"] == ["By the unanimous rule the panel's decision should be a mistrial."]
    # Split votes are still no winner, and a receipt that says so checks clean.
    split = panel_receipt(["buyer", "seller"], "unanimous", ["buyer", "seller", "buyer"], {"winner": None, "tally": {"buyer": 2, "seller": 1, "mistrial": 0}})
    assert problems(split) == []


def test_criteria_totals_keep_every_partys_own_key():
    # A dict has no prototype, so a party named "__proto__" always kept its total here; the JS client lost it until
    # its fix of Nov 10. The same receipts as the JS test.
    outcome = '{"winner":"__proto__","criteria":[{"name":"price","weight":1}],"scores":{"__proto__":{"price":9},"b":{"price":1}},"totals":{"__proto__":9,"b":1}}'
    one = single_receipt(["__proto__", "b"], json.loads(outcome))
    assert problems(one) == []
    wrong = copy.deepcopy(one)
    wrong["outcome"]["winner"] = "b"
    assert problems(wrong) == ["By the scores and agreed weights the winner should be __proto__."]
    # On a panel, each vote's pick comes from its own scores, and the tally keeps the party's own key too.
    extra = {"winner": "__proto__", "criteria": [{"name": "price", "weight": 1}], "tally": {"__proto__": 3, "b": 0, "mistrial": 0}}
    panel = panel_receipt(["__proto__", "b"], "majority", ["__proto__"] * 3, extra)
    for v in panel["outcome"]["votes"]:
        v["scores"] = {"__proto__": {"price": 9}, "b": {"price": 1}}
    assert problems(panel) == []


def test_a_score_counts_only_from_an_own_key():
    # The service counts a score only from an own key, and an empty one as 0, since its fix (deployed Nov 9); a dict
    # lookup always did. The same receipts as the JS test.
    criteria = [{"name": "__proto__", "weight": 5}, {"name": "price", "weight": 1}]
    scores = {"alpha": {"price": 0}, "beta": {"price": 10}}
    fixed = single_receipt(["alpha", "beta"], {"winner": "beta", "criteria": criteria, "scores": scores, "totals": {"alpha": 0, "beta": 10}})
    assert problems(fixed) == []
    empty_scores = {"alpha": {"price": None}, "beta": {"price": 10}}
    empty = single_receipt(["alpha", "beta"], {"winner": "beta", "criteria": criteria, "scores": empty_scores, "totals": {"alpha": 0, "beta": 10}})
    assert problems(empty) == []
    # What the service signed before its fix: null totals and the first party by name. That is caught.
    old = single_receipt(["alpha", "beta"], {"winner": "alpha", "criteria": criteria, "scores": scores, "totals": {"alpha": None, "beta": None}})
    assert problems(old) == [
        "The criteria totals don't match the judge's scores and the agreed weights.",
        "By the scores and agreed weights the winner should be beta.",
    ]
    # A review vote's weighted total follows the same rule; an own "__proto__" score does count.
    review = copy.deepcopy(REVIEW_3)
    review["criteria"] = criteria
    review["votes"] = [
        dict(v, scores={"__proto__": 2, "price": 10}, total=20) if i == 0 else dict(v, scores={"price": 7}, total=7)
        for i, v in enumerate(review["votes"])
    ]
    assert verify_review_outcome(review)["problems"] == []
    review["votes"][1]["total"] = 42
    assert verify_review_outcome(review)["problems"] == ["Vote 2's weighted total doesn't match its scores."]
