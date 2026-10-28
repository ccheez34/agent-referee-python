"""The README's runnable examples and the example scripts, run as written against the local stand-in service."""

import functools
import pathlib
import re
import runpy
import sys

import pytest

import agent_referee_client as arc

ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture
def pointed_at_mini(mini, monkeypatch, tmp_path):
    """Make `from agent_referee_client import AgentReferee` give a client for the stand-in service and its test key."""
    real = arc.AgentReferee

    class Local(real):
        def __init__(self, url=None, **kw):
            kw.setdefault("public_keys", [mini["key"]])
            kw.setdefault("poll_s", 0.1)
            super().__init__(mini["url"], **kw)

    monkeypatch.setattr(arc, "AgentReferee", Local)
    monkeypatch.setattr(arc, "verify_signature", functools.partial(arc.verify.verify_signature, public_keys=[mini["key"]]))
    monkeypatch.chdir(tmp_path)
    mini["reset"]()
    return mini


def readme_blocks():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    return re.findall(r"<!-- test -->\n```python\n(.*?)```", text, flags=re.S)


@pytest.mark.parametrize("code", readme_blocks(), ids=lambda c: c.splitlines()[0][:40])
def test_readme_examples_run(pointed_at_mini, code, capsys):
    ns = {}
    exec(compile(code, "README.md", "exec"), ns)
    if "is_genuine_review" in ns:
        r = arc.AgentReferee().review("Translate hola into English, one word.", "hello", free=True)
        assert ns["is_genuine_review"](r["receipt"], "Translate hola into English, one word.", "hello")
        assert not ns["is_genuine_review"](r["receipt"], "Translate hola into English, one word.", "Hello")
    out = capsys.readouterr().out
    assert "False" not in out, out  # every printed check is True


def test_example_scripts_run(pointed_at_mini, capsys, monkeypatch):
    runpy.run_path(str(ROOT / "examples" / "free_review.py"), run_name="__main__")
    runpy.run_path(str(ROOT / "examples" / "demo_match.py"), run_name="__main__")
    monkeypatch.setattr(sys, "argv", ["verify_receipt.py", "review-receipt.json"])
    runpy.run_path(str(ROOT / "examples" / "verify_receipt.py"), run_name="__main__")
    out = capsys.readouterr().out
    assert "check ok: True" in out and "signed by Agent Referee: True" in out and "False" not in out, out
