"""Sketch: a LangGraph agent with a tool for an independent sealed review before it hands work over.

pip install agent-referee-client langgraph langchain-core (plus your model's package). Not run in this release's
tests: check it against your LangGraph version.
"""
import json

from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from agent_referee_client import AgentReferee

ref = AgentReferee()


@tool
def independent_review(task: str, output: str) -> str:
    """Get an independent, signed review of your output against the task, from AI reviewers at other companies.
    Use it before delivering important work. Don't send secrets or personal data."""
    r = ref.review(task=task, output=output, free=True)
    with open(f"receipt-{r['review_id']}.json", "w", encoding="utf-8") as f:
        json.dump(r["receipt"], f)  # the only record of the review
    return json.dumps({"verdict": r["result"]["verdict"], "issues": [i for v in r["votes"] for i in v["issues"]], "receipt_ok": r["check"]["ok"]})


model = ...  # your chat model, e.g. from langchain_anthropic or langchain_openai
agent = create_react_agent(model, tools=[independent_review])
