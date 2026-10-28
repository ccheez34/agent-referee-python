r"""Cut the client's money-path code into six pieces for independent review, from the CURRENT source.

    py scripts\review\build_pieces.py

Writes scripts\review\pieces\piece-A.txt ... piece-F.txt. Each piece is the exact source text (with a header line
naming the file and lines), cut at function boundaries so it fits one review (at most 12,000 characters of output
and 4,000 of task, counted like JavaScript's .length). Run it right before sending, so the reviewers see the code
that's actually there. verify.py is left out: its checks are compared with the JavaScript client's in the tests.
"""
import hashlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "agent_referee_client"
HERE = pathlib.Path(__file__).resolve().parent
OUT = HERE / "pieces"


def utf16_len(s):
    return len(s.encode("utf-16-le")) // 2


def section(name, start=None, end=None):
    text = (SRC / name).read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    a = 0 if start is None else next(i for i, l in enumerate(lines) if l.startswith(start))
    b = len(lines) if end is None else next(i for i, l in enumerate(lines) if l.startswith(end))
    body = "".join(lines[a:b])
    return f"# ===== agent_referee_client/{name}, lines {a + 1}-{b} of {len(lines)} =====\n" + body


PIECES = {
    "A": [section("pay.py", None, "# ------")],
    "B": [section("pay.py", "# ------", None), section("_canonical.py")],
    "C": [section("client.py", "class AgentReferee:", "    def create(")],
    "D": [section("client.py", "    def create(", "    def _parse_invite(")],
    "E": [section("client.py", "    def _parse_invite(", "class Seat:")],
    "F": [section("client.py", None, "class AgentReferee:"), section("client.py", "class Seat:", None)],
}

OUT.mkdir(exist_ok=True)
ok = True
for k, parts in PIECES.items():
    text = "\n".join(parts)
    task = (HERE / "tasks" / f"task-{k}.txt").read_text(encoding="utf-8")
    with open(OUT / f"piece-{k}.txt", "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    n, t = utf16_len(text), utf16_len(task)
    fits = n <= 12000 and 10 <= len(task.strip()) and t <= 4000
    ok &= fits
    print(f"piece {k}: output {n:6d} chars, task {t:5d} chars, sha256 {hashlib.sha256(text.encode()).hexdigest()[:12]}  {'ok' if fits else 'TOO LONG'}")
if not ok:
    raise SystemExit("A piece doesn't fit one review. Nothing was written to the service; adjust the cut points.")
print(f"\nWrote {len(PIECES)} pieces to {OUT}")
