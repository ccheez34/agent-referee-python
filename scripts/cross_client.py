r"""Live, free: a Python seat against a JavaScript seat (the npm client's source), in one demo match.

    py scripts\cross_client.py --js-client C:\Users\Admin\Downloads\claud\agent-referee-client\src\index.mjs

Needs Node 18+. Uses 2 of today's 20 demo matches (each side creates the match with the same shared key).
"""
import argparse
import json
import os
import pathlib
import subprocess
import tempfile

from _common import both, check, finish

from agent_referee_client import PUBLIC_KEY, AgentReferee, canonical_json

ap = argparse.ArgumentParser()
ap.add_argument("--js-client", default=str(pathlib.Path(__file__).resolve().parents[2] / "agent-referee-client" / "src" / "index.mjs"))
args = ap.parse_args()
URL = os.environ.get("AGENT_REFEREE_URL", "https://agentreferee.ai")
KEY = os.environ.get("LIVE_TEST_KEY", PUBLIC_KEY)  # only for dry runs against a local copy of the server
js_url = pathlib.Path(args.js_client).resolve().as_uri()
key = "python-js-" + os.urandom(8).hex()
spec = {"tier": "weighted", "seats": ["alpha", "beta"], "weights": {"alpha": 60, "beta": 40}, "key": key}

js = f"""
import {{ AgentReferee }} from {json.dumps(js_url)};
const ref = new AgentReferee({{ url: {json.dumps(URL)}, demo: true, publicKeys: [{json.dumps(KEY)}] }});
const m = await ref.create({json.dumps(spec)});
const seat = await ref.join(m.match_id, "beta");
const st = await seat.play();
console.log(JSON.stringify({{ match_id: m.match_id, ok: st.check.ok, problems: st.check.problems, receipt: st.receipt }}));
"""
with tempfile.TemporaryDirectory() as d:
    f = pathlib.Path(d) / "js_seat.mjs"
    f.write_text(js, encoding="utf-8")
    ref = AgentReferee(URL, demo=True, public_keys=[KEY])

    def python_seat():
        m = ref.create(**spec)
        return m["match_id"], ref.join(m["match_id"], "alpha").play()

    def js_seat():
        p = subprocess.run(["node", str(f)], capture_output=True, text=True, timeout=600)
        if p.returncode:
            raise RuntimeError(p.stderr[-2000:])
        return json.loads(p.stdout.strip().splitlines()[-1])

    (mid, py), jsr = both(python_seat, js_seat)
check("both sides landed in the same match (shared key)", mid == jsr["match_id"], f"{mid} / {jsr['match_id']}")
check("Python seat: check.ok", py["check"]["ok"], "; ".join(py["check"]["problems"]))
check("JavaScript seat: check.ok", jsr["ok"], "; ".join(jsr["problems"]))
check("both hold the same signed receipt", canonical_json(py["receipt"]) == canonical_json(jsr["receipt"]))
print(f"      winner: {py['result']['winner']}")
finish()
