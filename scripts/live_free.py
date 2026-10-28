r"""Live, free checks against the real service. No wallet, nothing charged.

    py scripts\live_free.py

Uses 2 of today's 20 demo matches (weighted, then rps) and 1 of today's 3 free single-reviewer reviews.
Receipts are saved in scripts\live-data (not for the repo).
"""
import os

from _common import both, check, finish, save

from agent_referee_client import PUBLIC_KEY, AgentReferee, verify_review_outcome, verify_signature

URL = os.environ.get("AGENT_REFEREE_URL", "https://agentreferee.ai")
KEY = os.environ.get("LIVE_TEST_KEY", PUBLIC_KEY)  # only for dry runs against a local copy of the server
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live-data")
print(f"Live free checks against {URL}\n")

ref = AgentReferee(URL, public_keys=[KEY])
pub = ref.pubkey()
check("the service's signing key is the pinned one", pub.get("public_key") == KEY, pub.get("public_key"))
rv = ref.reviewers()
limit = (rv.get("long_inputs") or {}).get("max_chars_for_light_models")
check("GET /reviewers publishes the long-input limit", isinstance(limit, int), str(rv.get("long_inputs")))
check("free reviews are available", (rv.get("free") or {}).get("available") is True, str(rv.get("free")))

for tier, spec in [("weighted", {"weights": {"alpha": 70, "beta": 30}}), ("rps", {})]:
    a, b = AgentReferee(URL, demo=True, public_keys=[KEY]), AgentReferee(URL, demo=True, public_keys=[KEY])
    m = a.create(tier=tier, seats=["alpha", "beta"], title=f"Python client 0.1.0 live test ({tier})", **spec)
    ra, rb = both(lambda: a.join(m["match_id"], "alpha").play(), lambda: b.join(m["match_id"], "beta").play())
    check(f"demo {tier}: both seats finished", ra["phase"] == rb["phase"] == "done", f"{ra['phase']} / {rb['phase']}")
    check(f"demo {tier}: alpha's check.ok", ra["check"]["ok"], "; ".join(ra["check"]["problems"]))
    check(f"demo {tier}: beta's check.ok", rb["check"]["ok"], "; ".join(rb["check"]["problems"]))
    check(f"demo {tier}: both got the same signed receipt", ra["receipt"] == rb["receipt"])
    check(f"demo {tier}: the receipt says demo", ra["receipt"]["payload"].get("demo") is True)
    save(DATA, f"demo-{tier}-{m['match_id']}.json", {"receipt": ra["receipt"], "acks_alpha": ra["acks"], "acks_beta": rb["acks"]})
    print(f"      winner: {ra['result'].get('winner')}")

task = "Write a Python function is_leap(year) that returns True for leap years in the Gregorian calendar."
output = "def is_leap(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"
r = ref.review(task, output, free=True)
check("free review: check.ok (signature, exact text, recomputed verdict)", r["check"]["ok"], "; ".join(r["check"]["problems"]))
p = r["receipt"]["payload"]
check('free review: receipt says funding "free" and payer null', p.get("funding") == "free" and p.get("payer") is None, f"{p.get('funding')} / {p.get('payer')}")
check("free review: checks again offline with the pinned key", verify_signature(r["receipt"], [KEY]) and verify_review_outcome(r["receipt"], task=task, output=output, funding="free")["ok"])
check("free review: a changed output is caught", not verify_review_outcome(r["receipt"], task=task, output=output + " ")["ok"])
print(f"      verdict: {r['result']['verdict']} by {[v['model'] for v in r['votes']]}; free left today: {r['free']}")
save(DATA, f"free-review-{r['review_id']}.json", {"task": task, "output": output, "receipt": r["receipt"]})
finish()
