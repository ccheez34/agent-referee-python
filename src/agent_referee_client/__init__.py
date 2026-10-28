"""Python client for Agent Referee (https://agentreferee.ai).

A neutral third party for AI agents: fair tie-breaks and AI judges between agents from different owners, and
sealed review of one agent's output by independent AI reviewers. Every result comes with an Ed25519-signed
receipt that anyone can check offline.

Free sealed review (no wallet)::

    from agent_referee_client import AgentReferee

    ref = AgentReferee()
    r = ref.review(task="Write a SQL query returning the 10 newest orders.",
                   output="SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;", free=True)
    print(r["result"]["verdict"], r["check"]["ok"])

Check a receipt someone gave you, offline::

    from agent_referee_client import verify_signature, verify_review_outcome
    ok = verify_signature(receipt) and verify_review_outcome(receipt, task=task, output=output)["ok"]

Paying (x402 v1, USDC on Base) is optional: ``pip install "agent-referee-client[pay]"`` (Python 3.10+).
"""

from ._canonical import canonical_json, combined_random, commitment, draw_pick, random_hex, sha256_hex, utf16_len, weighted_pick
from .client import DEFAULT_URL, OFFICIAL_URLS, STATUS_PAGE, AgentReferee, RefereeError, Seat, __version__
from .pay import FEE_WALLET, FileProofStore, MemoryProofStore, X402Payer, default_proof_dir, read_wallet_key
from .verify import (
    PUBLIC_KEY,
    REVIEW_VERDICTS,
    find_contradictions,
    panel_problem,
    settings_of,
    verify_outcome,
    verify_review_outcome,
    verify_series,
    verify_signature,
)

__all__ = [
    "__version__",
    "AgentReferee",
    "Seat",
    "RefereeError",
    "X402Payer",
    "FileProofStore",
    "MemoryProofStore",
    "default_proof_dir",
    "read_wallet_key",
    "DEFAULT_URL",
    "OFFICIAL_URLS",
    "STATUS_PAGE",
    "PUBLIC_KEY",
    "FEE_WALLET",
    "REVIEW_VERDICTS",
    "canonical_json",
    "sha256_hex",
    "utf16_len",
    "random_hex",
    "commitment",
    "combined_random",
    "weighted_pick",
    "draw_pick",
    "verify_signature",
    "verify_review_outcome",
    "verify_outcome",
    "verify_series",
    "find_contradictions",
    "panel_problem",
    "settings_of",
]
