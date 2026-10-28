"""Sketch: a CrewAI tool for an independent sealed review. Not run in this release's tests: check it against your
CrewAI version. pip install agent-referee-client crewai"""
import json

from crewai.tools import tool

from agent_referee_client import AgentReferee

ref = AgentReferee()


@tool("Independent review")
def independent_review(task: str, output: str) -> str:
    """Get an independent, signed review of the output against the task from AI reviewers at other companies.
    Don't send secrets or personal data."""
    r = ref.review(task=task, output=output, free=True)
    return json.dumps({"verdict": r["result"]["verdict"], "issues": [i for v in r["votes"] for i in v["issues"]], "receipt_ok": r["check"]["ok"]})

# Then: Agent(role=..., goal=..., backstory=..., tools=[independent_review])
