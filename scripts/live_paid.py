r"""Live, PAID checks from a test wallet. The owner runs this; it spends real USDC on Base.

    py scripts\live_paid.py --wallet-file C:\Users\Admin\Downloads\test-wallet.txt

Costs $0.28 in all, each charged only if that step completes:
  review $0.05 + review-3 $0.20 + a weighted match with both seats from the same wallet 2 x $0.015 = $0.28.
The key is read from the wallet file and never printed. Receipts are saved in scripts\live-data (not for the repo).
"""
import argparse
import os

from _common import both, check, finish, save

from agent_referee_client import PUBLIC_KEY, AgentReferee, FileProofStore, X402Payer, verify_signature

ap = argparse.ArgumentParser()
ap.add_argument("--wallet-file", required=True)
ap.add_argument("--yes", action="store_true", help="don't ask before paying")
args = ap.parse_args()
URL = os.environ.get("AGENT_REFEREE_URL", "https://agentreferee.ai")
KEY = os.environ.get("LIVE_TEST_KEY", PUBLIC_KEY)  # only for dry runs against a local copy of the server
DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live-data")

payer = X402Payer.from_wallet_file(args.wallet_file, max_micro=200_000)
print(f"Paying from {payer.wallet} on Base, at most $0.20 per request, to {URL}.")


def preflight():
    """Sign a $0.01 authorization locally (sent nowhere) and confirm the signature recovers to this wallet."""
    import base64
    import json

    usdc = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    offer = {"x402Version": 1, "error": "preflight", "accepts": [{"scheme": "exact", "network": "base", "maxAmountRequired": "10000", "resource": "https://agentreferee.ai/preflight", "description": "local preflight, never sent", "mimeType": "application/json", "payTo": "0xF6B53AaCf822A36b7B2aF9C45BabE6E367ede4d3", "maxTimeoutSeconds": 60, "asset": usdc, "extra": {"name": "USD Coin", "version": "2"}}]}
    signed = json.loads(base64.b64decode(payer.payment_header(offer, {})))
    auth, sig = signed["payload"]["authorization"], signed["payload"]["signature"]
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError:
        return False, "eth_account isn't installed"
    typed = {
        "types": {
            "EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"}, {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
            "TransferWithAuthorization": [{"name": "from", "type": "address"}, {"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}, {"name": "validAfter", "type": "uint256"}, {"name": "validBefore", "type": "uint256"}, {"name": "nonce", "type": "bytes32"}],
        },
        "primaryType": "TransferWithAuthorization",
        "domain": {"name": "USD Coin", "version": "2", "chainId": 8453, "verifyingContract": usdc},
        "message": {"from": auth["from"], "to": auth["to"], "value": int(auth["value"]), "validAfter": int(auth["validAfter"]), "validBefore": int(auth["validBefore"]), "nonce": auth["nonce"]},
    }
    who = Account.recover_message(encode_typed_data(full_message=typed), signature=sig)
    ok = who.lower() == payer.wallet.lower() and signed["x402Version"] == 1 and signed["network"] == "base" and auth["value"] == "10000"
    return ok, f"recovered {who}, header {signed['x402Version']}/{signed['network']}/{auth['value']}"


ok, detail = preflight()
check("preflight: x402 signs a v1 USDC authorization that recovers to this wallet (signed locally, never sent)", ok, detail)
if not ok:
    raise SystemExit("Stopped before paying anything. Nothing was charged.")
print("This run costs $0.28: review $0.05 + review-3 $0.20 + weighted match 2 x $0.015.")
if not args.yes and input('Type "pay" to go ahead: ').strip().lower() != "pay":
    raise SystemExit("Stopped. Nothing was charged.")
store = FileProofStore(os.path.join(DATA, "proofs"))  # this run's own folder, so other runs can't interfere
ref = AgentReferee(URL, payer=payer, proof_store=store, public_keys=[KEY])
print(f"(payment proofs for joins go to {store.directory} and are deleted once the service answers)\n")

task = "Write a SQL query returning the 10 newest orders from a table orders(id, created_at, total)."
output = "SELECT id, created_at, total FROM orders ORDER BY created_at DESC LIMIT 10;"
for tier, panel in [("review", None), ("review-3", "rotate")]:
    r = ref.review(task, output, tier=tier, panel=panel)
    p = r["receipt"]["payload"]
    check(f"paid {tier}: check.ok", r["check"]["ok"], "; ".join(r["check"]["problems"]))
    check(f"paid {tier}: the receipt names this wallet as payer, no funding field", p.get("payer") == payer.wallet.lower() and "funding" not in p, f"{p.get('payer')} / {p.get('funding')}")
    check(f"paid {tier}: the service reported the settlement", bool(r["payment"]["transaction"]), str(r["payment"]))
    check(f"paid {tier}: checks again offline", verify_signature(r["receipt"], [KEY]))
    print(f"      verdict {r['result']['verdict']} by {[v['model'] for v in r['votes']]}, transaction {r['payment']['transaction']}")
    save(DATA, f"paid-{tier}-{r['review_id']}.json", {"task": task, "output": output, "receipt": r["receipt"], "payment": r["payment"]})

a = AgentReferee(URL, payer=payer, proof_store=store, public_keys=[KEY])
b = AgentReferee(URL, payer=payer, proof_store=store, public_keys=[KEY])
m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50}, title="Python client 0.1.0 paid live test")
ra, rb = both(lambda: a.join(m["match_id"], "alpha").play(), lambda: b.join(m["match_id"], "beta").play())
check("paid weighted: alpha's check.ok", ra["check"]["ok"], "; ".join(ra["check"]["problems"]))
check("paid weighted: beta's check.ok", rb["check"]["ok"], "; ".join(rb["check"]["problems"]))
paid_by = ra["receipt"]["payload"].get("paid_by") or {}
check("paid weighted: both seats paid by this wallet", paid_by.get("alpha") == paid_by.get("beta") == payer.wallet.lower(), str(paid_by))
check("paid weighted: not a demo receipt", not ra["receipt"]["payload"].get("demo"))
check("paid weighted: no payment proofs left behind", store.list() == [], str(len(store.list())))
save(DATA, f"paid-weighted-{m['match_id']}.json", {"receipt": ra["receipt"], "acks_alpha": ra["acks"], "acks_beta": rb["acks"]})
print(f"      winner {ra['result']['winner']}")
finish()
