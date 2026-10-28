r"""A paid review-3 ($0.20, charged only if it completes).

Run: py examples/paid_review.py C:\path\to\wallet.txt
The wallet file holds the private key of a dedicated wallet with a little USDC on Base. It's read, never printed.
"""
import sys

from agent_referee_client import AgentReferee, X402Payer

payer = X402Payer.from_wallet_file(sys.argv[1], max_micro=200_000)  # never more than $0.20 per request
ref = AgentReferee(payer=payer)
r = ref.review(
    task="Write a SQL query returning the 10 newest orders.",
    output="SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;",
    tier="review-3",
    panel="rotate",
)
print(r["result"]["verdict"], r["result"]["tally"], "| check ok:", r["check"]["ok"])
print("paid from", r["payment"]["wallet"], "transaction", r["payment"]["transaction"])
