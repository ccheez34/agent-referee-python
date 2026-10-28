"""A free sealed review: no wallet, nothing charged. Run: py examples/free_review.py"""
import json

from agent_referee_client import AgentReferee

ref = AgentReferee()
task = "Write a Python function is_leap(year) that returns True for leap years in the Gregorian calendar."
output = "def is_leap(year):\n    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)\n"

r = ref.review(task=task, output=output, free=True)
print("verdict:", r["result"]["verdict"], "| receipt check ok:", r["check"]["ok"], "| free left today:", r["free"]["left_today"])
for v in r["votes"]:
    print(f"- {v['model']}: {v['verdict']} {v['issues']}")

# The receipt is the only record: keep it with the exact task and output.
with open("review-receipt.json", "w", encoding="utf-8") as f:
    json.dump({"task": task, "output": output, "receipt": r["receipt"]}, f, ensure_ascii=False, indent=1)
