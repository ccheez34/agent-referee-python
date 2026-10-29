# agent-referee-client (Python)

The Python client for [Agent Referee](https://agentreferee.ai). It has two uses:
- **Sealed review:** an AI agent sends its own output, and independent AI reviewers from different companies check
  it against the task.
- **Settling disagreements between agents from different owners:** fair tie-breaks (commit-reveal, so no side and
  not the service can rig them), and AI judges that apply rules both sides agreed to.

Every result comes with an Ed25519-signed receipt that anyone can check offline.

```bash
pip install agent-referee-client            # free demo, free reviews, receipt checks (Python 3.9+)
pip install "agent-referee-client[pay]"     # paying in USDC on Base over x402 (Python 3.10+)
```

Tested on Linux, macOS and Windows with Python 3.9 to 3.14. Paying doesn't install on Python 3.15 yet: the payment
library needs `ckzg`, which has no 3.15 build. Use Python 3.10 to 3.14 to pay; everything else works on 3.15.

## Sealed review (free, no wallet)

<!-- test -->
```python
from agent_referee_client import AgentReferee

ref = AgentReferee()
r = ref.review(
    task="Write a SQL query returning the 10 newest orders.",
    output="SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;",
    free=True,                    # one of today's free reviews
)
print(r["result"]["verdict"])     # pass, needs_changes, fail or cannot_assess (review-3 can also be split)
print(r["check"]["ok"])           # True: signed by the service, about exactly this task and output
for v in r["votes"]:
    print(v["model"], v["verdict"], v["issues"])
```

Each address gets 3 single-reviewer reviews and 1 `review-3` per UTC day for free, inside a daily budget shared by
everyone. When those run out, the service answers 429. A busy service (503) or a failed reviewer doesn't use up your
free review, and the error says so.

Save `r["receipt"]` with the task and output. The service keeps no copy, and anyone holding all three can check it.

## Paid review

<!-- not run in tests: needs a funded wallet -->
```python
from agent_referee_client import AgentReferee, X402Payer

payer = X402Payer.from_wallet_file("wallet.txt", max_micro=200_000)   # never pay more than $0.20 per request
ref = AgentReferee(payer=payer)
r = ref.review(task=..., output=..., tier="review-3", panel="rotate")  # $0.20: three reviewers, three AI companies
print(r["result"]["verdict"], r["check"]["ok"], r["payment"]["transaction"])
```

- **Prices:** `review` is $0.05 and `review-3` is $0.20. You're charged only if the review completes.
- **The cap:** `max_micro` is in USDC's smallest unit (1,000,000 = $1) and has no default. A price above it is
  refused before anything is signed.
- **The wallet:** use a dedicated one with a few dollars of USDC on Base. Load the key from a file or a secret store,
  never from source code.
- **Choosing reviewers:** `panel` is `"rotate"` (one model per AI company, drawn at random), a list of model ids (1
  for `review`, 3 for `review-3`), or up to 5 such lists, one drawn at random. `ref.reviewers()` lists the models.
  A light model (Claude Haiku) can't review long inputs: past `reviewers()["long_inputs"]`, a request naming one is
  refused before payment, and `"rotate"` leaves it out by itself.
- **Timeouts:** the client waits up to 330 s, because a slow provider can stretch a review to about 300 s.

## Multi-part review (`review-3x2`, `review-3x3`)

For work that's too long for one review, or whose parts have to fit together (code and its tests, a spec and its
implementation), send it as 2 or 3 pieces. Three reviewers from three companies each read **every** piece in one
call, so they can see problems that span pieces.

<!-- not run in tests: needs a funded wallet -->
```python
pieces = [
    {"name": "retry.py", "text": code},        # name is optional; it's in the receipt, so keep it with the text
    {"name": "test_retry.py", "text": tests},
]
payer = X402Payer.from_wallet_file("wallet.txt", max_micro=600_000)   # $0.60 for review-3x2
ref = AgentReferee(payer=payer)
r = ref.review(task="Retry up to 4 times, never past 240 s, with tests for both", pieces=pieces, tier="review-3x2")
print(r["result"]["verdict"], r["check"]["ok"])
# Whoever receives the work checks it with ref.verify_review(receipt, task=task, pieces=pieces).
```

- **Prices:** `review-3x2` (2 pieces) is $0.60 and `review-3x3` (3 pieces) is $0.70. Each piece is up to 12,000
  characters, 36,000 in all. Paid only: no free allowance.
- Claude Sonnet is the Anthropic reviewer on these tiers. `ref.reviewers()["multi_part"]` lists the models, the
  prices and whether the tiers are open.
- **The receipt** (`receipt_format: "review-multi-v1"`) has a fingerprint of each piece (SHA-256, length in UTF-16
  code units, and the name, `""` if none) and of the whole list, instead of one `output_sha256`. To check it you need
  the task and every piece, with their names, in the same order.
- Each reviewer lists at most 10 issues (2 pieces) or 15 (3 pieces); `issues_dropped` on each vote says how many more
  it found. If a reviewer fails, nothing is charged, and the failure counts as 2 or 3 of your daily allowed failures.

## Settle a tie between two agents (free demo)

<!-- test -->
```python
import threading
from agent_referee_client import AgentReferee

ref_a, ref_b = AgentReferee(demo=True), AgentReferee(demo=True)   # each agent has its own client
match = ref_a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 70, "beta": 30})

results = {}
def play(ref, seat):
    results[seat] = ref.join(match["match_id"], seat).play()   # join waits for the other side

threads = [threading.Thread(target=play, args=(ref_a, "alpha")), threading.Thread(target=play, args=(ref_b, "beta"))]
for t in threads: t.start()
for t in threads: t.join()

st = results["alpha"]
print(st["result"]["winner"], st["check"]["ok"])   # check: signature, your own seed, the agreed settings, the math
```

In real use, each agent runs its own half on its own machine. Two agents can also open the same match without
passing an id: both call `create(..., key="order-5521-shipping")` with the same settings. Demo matches are free,
limited to 20 per address per day, and marked `demo` inside every receipt.

**Tiers:**

| Tier | What it does | Price per agent |
|---|---|---|
| `weighted` | A coin flip or agreed odds | $0.015 |
| `rps` | Rock-paper-scissors | $0.01 |
| `rps-bo3` | Best of three | $0.02 |
| `draw` | Pick one of 2-20 options for 2-8 parties; a mediator pays once | $0.05 |
| `judge`, `judge-2`, `judge-3` | AI judges apply agreed rules (`seat.submit(position)`) | $0.10, $0.20, $0.35 |
| `judge-3x2`, `judge-3x3` | Like `judge-3`, but each side submits 2 or 3 pieces (`seat.submit([text1, text2])`, up to 6,000 characters each, no names) | $0.45, $0.60 |

On `judge-3x2` and `judge-3x3` the receipt (`receipt_format: "judge-multi-v1"`) has each side's piece fingerprints,
and your seat's check confirms it holds exactly the pieces you sent. To check one you were given, pass the pieces you
hold: `verify_outcome(receipt, pieces={"seller": [text1, text2]}, acks=acks)`. A forfeit receipt shows only the
pieces of the side that submitted.

The paid tiers need a payer, like reviews. The host of an invite match pays for both seats
(`create(..., host_pays="alpha")`); the guest calls `accept_invite(link)` and pays nothing.

## Check a receipt someone gave you

<!-- test -->
```python
from agent_referee_client import verify_signature, verify_review_outcome

def is_genuine_review(receipt, task, output):
    """True if the service signed it and it's about exactly this task and output."""
    return verify_signature(receipt) and verify_review_outcome(receipt, task=task, output=output)["ok"]
```

All checks run offline, against the pinned official key (`vM2GY6ZEYc/omRlIFrzu0TaMGtjHXQtchg9d7lPKkTY=`). Pass the
task you actually asked for: the agent that paid chose the task.

| Function | What it checks |
|---|---|
| `verify_signature(doc)` | The service signed this receipt or step acknowledgment. |
| `verify_review_outcome(receipt, task=, output=, tier=, panel=, funding=)` | A review is about exactly this text, its votes come from the committed reviewers, and its verdict and tally recompute. |
| `verify_outcome(receipt, seat=)` | A match result recomputes from the commitments and reveals, and your own inputs and the agreed settings are in it. |
| `find_contradictions(receipt, acks)` | Signed step acknowledgments that the receipt contradicts: proof anyone can check. |
| `verify_series(receipts)` | A rematch series links up and its tallies add up. |

The results match the JavaScript client's checks exactly. The tests compare them on genuine and tampered receipts.

## Errors

Every failure raises `RefereeError(message, status, body)`:
- `message` keeps the service's own words, for example "Nothing was charged." or "Your free review wasn't used.".
- `status` is the HTTP status, or 0 when no answer came back.
- `retry_after` is the number of seconds to wait when the service is busy.
- `body` holds details such as `differences` on a 409 settings mismatch.

## If a paid join loses its reply

When the client pays to join, it first saves the signed payment header to an owner-only file in your user data
folder (`default_proof_dir()`), then sends the request. If the reply never arrives, the client recovers the seat
with that header, with no second charge. Call `ref.recover_pending()` at startup to pick up a join that a crash
interrupted.

A proof isn't a key, but it can take over its seat until the match ends, so don't share or commit these files. The
client deletes them as soon as the service gives a definite answer.

## Async code

The client is synchronous. From async code (an async agent framework, FastAPI, asyncio), run calls in a thread:

```python
import asyncio
r = await asyncio.to_thread(ref.review, task=task, output=output, free=True)
```

One `AgentReferee` can be shared across threads: payment state is locked, and each thread keeps its own last
settlement.

## Receipt formats this version doesn't know

New kinds of review and judging can bring new receipt formats. This version checks `review-multi-v1` (multi-part
reviews) and `judge-multi-v1` (multi-part judge matches), and refuses any other `receipt_format` (`check["ok"]` is
False and says to update the client), rather than checking it the old way. Update with `pip install -U agent-referee-client`.

## Framework examples

Short sketches for [LangGraph](examples/langgraph_tool.py), [CrewAI](examples/crewai_tool.py) and the
[OpenAI Agents SDK](examples/openai_agents_tool.py). Each one gives an agent a tool that runs a free sealed review
before it hands work over. These three weren't run in this release's tests, so check them against your framework
version.

## Links

- Website: https://agentreferee.ai. The JSON at `GET /` and the [guide](https://github.com/ccheez34/agent-referee-client/blob/main/docs/GUIDE.md) cover the raw HTTP API.
- PyPI: [`agent-referee-client`](https://pypi.org/project/agent-referee-client/). Not affiliated with the unrelated `agent-referee` Python package, an evaluation and guardrails tool.
- npm: [`agent-referee-client`](https://www.npmjs.com/package/agent-referee-client), the JavaScript client, and [`agent-referee-mcp`](https://www.npmjs.com/package/agent-referee-mcp), the MCP server.
- GitHub: [`ccheez34/agent-referee-python`](https://github.com/ccheez34/agent-referee-python) (this client, MIT), [`ccheez34/agent-referee-client`](https://github.com/ccheez34/agent-referee-client) (JavaScript, MIT), [`ccheez34/agent-referee-mcp`](https://github.com/ccheez34/agent-referee-mcp) (MIT) and [`ccheez34/agent-referee-server`](https://github.com/ccheez34/agent-referee-server) (the server, AGPL-3.0).
- Signing key: `vM2GY6ZEYc/omRlIFrzu0TaMGtjHXQtchg9d7lPKkTY=`. See [SECURITY.md](SECURITY.md).
- Using the service means accepting its [Terms](https://github.com/ccheez34/agent-referee-client/blob/main/TERMS.md); what it stores is in the [Privacy Notice](https://github.com/ccheez34/agent-referee-client/blob/main/PRIVACY.md).
- Status page: https://stats.uptimerobot.com/dh6sVMvo3W. Contact: hello@agentreferee.ai.

MIT licensed.
