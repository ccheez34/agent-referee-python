"""Sketch: an OpenAI Agents SDK agent with a tool for an independent sealed review. Not run in this release's tests:
check it against your SDK version. pip install agent-referee-client openai-agents"""
import json

from agents import Agent, Runner, function_tool  # noqa: F401  (Runner: see the last line)

from agent_referee_client import AgentReferee

ref = AgentReferee()


@function_tool
def independent_review(task: str, output: str) -> str:
    """Get an independent, signed review of the output against the task from AI reviewers at other companies.
    Don't send secrets or personal data."""
    r = ref.review(task=task, output=output, free=True)
    return json.dumps({"verdict": r["result"]["verdict"], "issues": [i for v in r["votes"] for i in v["issues"]], "receipt_ok": r["check"]["ok"]})


agent = Agent(name="Writer", instructions="Before delivering a final answer, get it independently reviewed and fix any issues.", tools=[independent_review])
# result = Runner.run_sync(agent, "Write a SQL query returning the 10 newest orders.")
