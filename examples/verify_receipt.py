"""Check a review receipt someone sent you, offline. Run: py examples/verify_receipt.py review-receipt.json"""
import json
import sys

from agent_referee_client import verify_review_outcome, verify_signature

with open(sys.argv[1], encoding="utf-8") as f:
    d = json.load(f)
signed = verify_signature(d["receipt"])  # the pinned official key
check = verify_review_outcome(d["receipt"], task=d["task"], output=d["output"])
print("signed by Agent Referee:", signed)
print("about exactly this task and output, verdict recomputed:", check["ok"], check["problems"])
print("verdict:", d["receipt"]["payload"]["result"]["verdict"])
