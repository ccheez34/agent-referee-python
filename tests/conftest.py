"""Shared fixtures.

``mini`` starts tests/js/mini_server.mjs: the real server code for matches, acks, receipts and reviews (with fake
AI answers and emulated payments) on 127.0.0.1. It needs Node 22.6+ and the server source, found at
AGENT_REFEREE_SERVER_SRC or ../agent-referee-server/src next to this project. Without them those tests are skipped.
"""

import json
import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent


def server_src():
    env = os.environ.get("AGENT_REFEREE_SERVER_SRC")
    for c in [env, ROOT.parent / "agent-referee-server" / "src"]:
        if c and (pathlib.Path(c) / "match.ts").exists():
            return pathlib.Path(c)
    return None


def node_ok():
    node = shutil.which("node")
    if not node:
        return None
    try:
        v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip().lstrip("v")
        major, minor = (int(x) for x in v.split(".")[:2])
    except Exception:
        return None
    return node if (major, minor) >= (22, 6) else None


@pytest.fixture(scope="session")
def mini():
    src, node = server_src(), node_ok()
    if not src or not node:
        pytest.skip("needs Node 22.6+ and the server source (set AGENT_REFEREE_SERVER_SRC)")
    p = subprocess.Popen(
        [node, "--experimental-strip-types", "--no-warnings", str(ROOT / "tests" / "js" / "mini_server.mjs"), str(src)],
        stdout=subprocess.PIPE,
        stderr=sys.stderr,
        text=True,
    )
    try:
        info = json.loads(p.stdout.readline())
        yield {"url": f"http://127.0.0.1:{info['port']}", "key": info["public_key"], "reset": lambda: reset(info["port"])}
    finally:
        p.terminate()
        try:
            p.wait(timeout=10)
        except Exception:
            p.kill()


def reset(port):
    """Give the stand-in service's free-review allowance back (it counts per run, like the real one per day)."""
    import urllib.request

    req = urllib.request.Request(f"http://127.0.0.1:{port}/__test", data=b'{"resetFree": true}', method="POST", headers={"content-type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()


def have_x402():
    try:
        import x402  # noqa: F401
        from x402.http import x402HTTPClientSync  # noqa: F401
    except Exception:
        return False
    return sys.version_info >= (3, 10)


class FakeScheme:
    """Stands in for x402's v1 "exact" EVM scheme in tests: same shape of payload, a dummy signature, no wallet."""

    scheme = "exact"

    def __init__(self, address="0x1111111111111111111111111111111111111111"):
        self.address = address
        self.signed = []

    @staticmethod
    def find_default_asset(asset, network):
        # USDC on Base, as x402's own table lists it (the spend controls only allow known assets).
        if asset.lower() == "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913" and network in ("base", "eip155:8453"):
            return {"asset": asset, "symbol": "USDC", "decimals": 6, "name": "USD Coin", "version": "2"}
        return None

    def create_payment_payload(self, requirements):
        self.signed.append(int(requirements.max_amount_required))
        return {
            "authorization": {
                "from": self.address,
                "to": requirements.pay_to,
                "value": requirements.max_amount_required,
                "validAfter": "0",
                "validBefore": "9999999999",
                "nonce": "0x" + os.urandom(32).hex(),
            },
            "signature": "0x" + "11" * 65,
        }


@pytest.fixture
def fake_payer():
    if not have_x402():
        pytest.skip('needs Python 3.10+ and the x402 package (pip install -e ".[dev,pay]")')
    from agent_referee_client import X402Payer

    def make(max_micro=200_000, address="0x1111111111111111111111111111111111111111", **kw):
        scheme = FakeScheme(address)
        payer = X402Payer(None, max_micro, _scheme=scheme, **kw)
        payer.scheme = scheme
        return payer

    return make
