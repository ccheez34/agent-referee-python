"""Offline checks of the service's signed documents. No network, no client needed.

Each function mirrors the JavaScript client 0.8.0 (``agent-referee-client`` on npm) condition by condition, so a
receipt passes or fails ``check["ok"]`` the same way in both languages.
"""

from __future__ import annotations

import base64
import binascii
import copy
import math
import re
from typing import Any, Dict, Iterable, List, Optional

from ._canonical import _utf16_key, canonical_json, sha256_hex, utf16_len

__all__ = [
    "PUBLIC_KEY",
    "REVIEW_VERDICTS",
    "verify_signature",
    "verify_review_outcome",
    "verify_outcome",
    "verify_series",
    "find_contradictions",
    "panel_problem",
]

#: The service's receipt-signing key (Ed25519, base64; key ID 375ba8b346776e2f). Pin it rather than fetching it.
PUBLIC_KEY = "vM2GY6ZEYc/omRlIFrzu0TaMGtjHXQtchg9d7lPKkTY="

REVIEW_VERDICTS = ["pass", "needs_changes", "fail", "cannot_assess"]
JUDGE_TIERS = ["judge", "judge-2", "judge-3", "judge-3x2", "judge-3x3"]
#: Multi-part judge tiers: how many pieces each side submits. Three judges from three companies read every piece.
JUDGE_MULTI_PIECES = {"judge-3x2": 2, "judge-3x3": 3}
JUDGE_FORMAT = "judge-multi-v1"
JUDGE_MULTI_FORMAT_VERSION = "agent-referee-judge-v5-multi"
_REVIEW_SIZE = {"review": 1, "review-3": 3, "review-3x2": 3, "review-3x3": 3}
_RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


class _Undefined:
    """Stands in for JavaScript's undefined (a missing field), which compares differently from null."""

    def __repr__(self) -> str:
        return "undefined"

    def __bool__(self) -> bool:
        return False


UNDEF: Any = _Undefined()


def _get(d: Any, key: Any) -> Any:
    """d[key], or UNDEF when d isn't an object/list or has no such key (JavaScript's optional chaining)."""
    if isinstance(d, dict):
        return d[key] if key in d else UNDEF
    if isinstance(d, list) and isinstance(key, int) and not isinstance(key, bool):
        return d[key] if 0 <= key < len(d) else UNDEF
    return UNDEF


def _nn(v: Any, default: Any) -> Any:
    """JavaScript's ``v ?? default``."""
    return default if v is None or v is UNDEF else v


def _truthy(v: Any) -> bool:
    """JavaScript truthiness: [] and {} are true; 0, "", null, undefined and NaN are false."""
    if v is UNDEF or v is None or v is False:
        return False
    if isinstance(v, (list, dict)):
        return True
    if isinstance(v, float) and math.isnan(v):
        return False
    return bool(v)


def _same(a: Any, b: Any) -> bool:
    """JavaScript's ``a === b`` for the values receipts hold."""
    if a is UNDEF or b is UNDEF:
        return a is b
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if isinstance(a, (dict, list)) or isinstance(b, (dict, list)):
        return a is b
    return False


def _s(v: Any) -> str:
    """A value inside a JavaScript template string (`${v}`)."""
    if v is UNDEF:
        return "undefined"
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, (int, float)):
        return canonical_json(v)
    if isinstance(v, list):
        return ",".join("" if x is None or x is UNDEF else _s(x) for x in v)
    if isinstance(v, dict):
        return "[object Object]"
    return str(v)


def _jsort(xs: Iterable[str]) -> List[str]:
    """JavaScript's default sort for strings (UTF-16 code units)."""
    return sorted(xs, key=lambda x: _utf16_key(_s(x)))


def _canon(v: Any) -> str:
    return "undefined" if v is UNDEF else canonical_json(_strip_undef(v))


def _strip_undef(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _strip_undef(x) for k, x in v.items() if x is not UNDEF}
    if isinstance(v, list):
        return [None if x is UNDEF else _strip_undef(x) for x in v]
    if isinstance(v, float) and math.isnan(v):
        return None
    return v


def _payload(doc: Any) -> Any:
    p = _get(doc, "payload")
    return doc if p is None or p is UNDEF else p


# ---------------------------------------------------------------------------------------------------------------
# Signatures
# ---------------------------------------------------------------------------------------------------------------

_B64 = re.compile(r"^[A-Za-z0-9+/]*$")


def _b64decode(s: str) -> bytes:
    """Base64 the way JavaScript's atob reads it (the "forgiving" rules): whitespace ignored, padding optional."""
    s = re.sub(r"[\t\n\f\r ]", "", s)
    if len(s) % 4 == 0 and s.endswith("="):
        s = s[:-2] if s.endswith("==") else s[:-1]
    if len(s) % 4 == 1 or not _B64.match(s):
        raise ValueError("invalid base64")
    return base64.b64decode(s + "=" * (-len(s) % 4), validate=True)


def verify_signature(doc: Any, public_keys: Iterable[str] = (PUBLIC_KEY,)) -> bool:
    """True if a receipt or step acknowledgment was signed by one of ``public_keys`` (base64 Ed25519 keys).

    Offline: no network. Defaults to the pinned official key. This checks who signed the document; the other
    functions here check what it says.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if isinstance(public_keys, str):
        public_keys = [public_keys]
    if not _truthy(_get(doc, "signature")) or not _truthy(_get(doc, "payload")):
        return False
    trusted = [k for k in public_keys if k]
    named = _get(doc, "public_key")
    candidates = [k for k in trusted if k == named] if _truthy(named) else trusted
    try:
        data = canonical_json(doc["payload"]).encode("utf-8")
        sig = _b64decode(doc["signature"])
    except (TypeError, ValueError, binascii.Error):
        return False
    for k in candidates:
        try:
            Ed25519PublicKey.from_public_bytes(_b64decode(k)).verify(sig, data)
            return True
        except (InvalidSignature, ValueError, TypeError, binascii.Error):
            continue
    return False


# ---------------------------------------------------------------------------------------------------------------
# Panels
# ---------------------------------------------------------------------------------------------------------------


def _company(m: Any) -> str:
    m = _s(m)
    return m.split("/")[0] if "/" in m else "anthropic" if m.startswith("claude") else "other"


def panel_problem(panel: Any, models: List[str], selection: Any) -> Optional[str]:
    """Did the panel the service used honour what was asked for? Returns a problem, or None.

    ``panel`` is the request ("rotate", a list of models, a list of model sets, or ``{"picks": {...}}``);
    ``models`` and ``selection`` come from the receipt (``reviewers.models`` / ``panel_selection``, or the judges).
    """
    if not _truthy(panel):
        return None
    mode = _get(selection, "mode")
    if isinstance(panel, dict):
        if not _same(mode, "picks") or _canon(_nn(_get(selection, "picks"), None)) != _canon(_nn(_get(panel, "picks"), None)):
            return "the agreed picks weren't used"
        picks = _nn(_get(panel, "picks"), {})
        picked = list(picks.values()) if isinstance(picks, dict) else []
        if len(models) != len(picked) + 1 or not all(m in models for m in picked):
            return "a party's pick isn't on the panel"
        third = [m for m in models if m not in picked]
        pool = _nn(_get(selection, "pool"), [])
        if len(third) != 1 or not isinstance(pool, list) or third[0] not in pool:
            return "the drawn judge isn't from the published pool"
        if len({_company(m) for m in models}) != len(models):
            return "the panel repeats an AI company"
        return None
    if panel == "rotate":
        if not _same(mode, "rotate"):
            return "the agreed rotating panel wasn't used"
        pool = _nn(_get(selection, "pool"), [])
        if not all(isinstance(pool, list) and m in pool for m in models):
            return "a rotating-panel model isn't from the published pool"
        if len({_company(m) for m in models}) != len(models):
            return "the rotating panel repeats an AI company"
        return None
    if not isinstance(panel, list):
        return "the panel request isn't one the service accepts"
    if all(isinstance(x, str) for x in panel):
        return None if _canon(models) == _canon(panel) else "the panel's models differ from the agreed models"
    if not _same(mode, "random_set") or _canon(_get(selection, "options")) != _canon(panel):
        return "the panel wasn't drawn from the agreed sets"
    return None if any(_canon(s) == _canon(models) for s in panel) else "the panel isn't one of the agreed sets"


# ---------------------------------------------------------------------------------------------------------------
# Sealed review
# ---------------------------------------------------------------------------------------------------------------


MULTI_FORMAT = "review-multi-v1"
#: Multi-part review tiers and how many pieces each takes. Three reviewers from three companies read every piece.
MULTI_PIECES = {"review-3x2": 2, "review-3x3": 3}

#: Receipt formats this client can check, by kind of receipt. Single-piece receipts have no ``receipt_format`` field;
#: multi-part reviews are ``review-multi-v1`` and multi-part judge matches ``judge-multi-v1``. Any other format (or a
#: format on the wrong kind of receipt) is refused, never checked the old way: an old check could recompute the
#: verdict and miss what the new format changed.
KNOWN_RECEIPT_FORMATS: Dict[str, tuple] = {"review": (MULTI_FORMAT,), "match": (JUDGE_FORMAT,)}


def _format_problem(p: Any, kind: str = "match") -> Optional[str]:
    known = KNOWN_RECEIPT_FORMATS.get(kind, ())
    if isinstance(p, dict) and "receipt_format" in p and not (isinstance(p["receipt_format"], str) and p["receipt_format"] in known):
        return (
            f"This receipt uses the {_s(p['receipt_format'])[:60]} format, which this version of agent-referee-client "
            "can't check, so nothing was confirmed. Update the client (pip install -U agent-referee-client)."
        )
    return None


def _is_int(v: Any) -> bool:
    """A whole number as JavaScript's Number.isInteger sees it (31 or 31.0, never a bool)."""
    return not isinstance(v, bool) and (isinstance(v, int) or (isinstance(v, float) and v.is_integer()))


def _check_pieces(p: Any, output: Optional[str], pieces: Any, problems: List[str]) -> None:
    """The multi-part part of a ``review-multi-v1`` check: the receipt's piece list is well formed, ``pieces_sha256``
    is the hash of it, there's no ``output_sha256``, and (when you pass the pieces you hold) each piece's name, sha256
    and chars match, in order. Pieces are ``{"name"?: str, "text": str}``; a piece without a name is recorded as ""."""
    n = MULTI_PIECES[_get(p, "tier")]
    if isinstance(p, dict) and "output_sha256" in p:
        problems.append("A multi-part receipt never has output_sha256; this one does.")
    if output is not None:
        problems.append("This is a multi-part review: check it with pieces, not output.")
    recs = _get(p, "pieces")
    if not isinstance(recs, list) or len(recs) != n:
        problems.append(f"A {_get(p, 'tier')} receipt lists {n} pieces; this one lists {len(recs) if isinstance(recs, list) else 'none'}.")
        return
    for i, r in enumerate(recs):
        ok = (
            isinstance(r, dict)
            and _same(_get(r, "index"), i + 1)
            and isinstance(_get(r, "name"), str)
            and isinstance(_get(r, "sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", _get(r, "sha256")) is not None
            and _is_int(_get(r, "chars"))
            and _get(r, "chars") > 0
        )
        if not ok:
            problems.append(f"Piece {i + 1}'s record in the receipt is malformed.")
    if not _same(sha256_hex(_canon(recs)), _get(p, "pieces_sha256")):
        problems.append("pieces_sha256 doesn't match the receipt's list of pieces.")
    if pieces is None:
        return
    if not isinstance(pieces, (list, tuple)) or len(pieces) != n:
        problems.append(f"This review has {n} pieces; you passed {len(pieces) if isinstance(pieces, (list, tuple)) else 'something that is not a list'}.")
        return
    texts = [g.get("text") if isinstance(g, dict) else None for g in pieces]
    hashes = [sha256_hex(t) if isinstance(t, str) else None for t in texts]
    rec_hashes = [_s(_get(r, "sha256")) for r in recs]
    if all(h is not None for h in hashes) and sorted(hashes) == sorted(rec_hashes) and hashes != rec_hashes:
        problems.append("These are the review's pieces, but in a different order.")
        return
    for i, g in enumerate(pieces):
        r = recs[i]
        if hashes[i] is None:
            problems.append(f"Piece {i + 1} you passed has no text.")
            continue
        if hashes[i] != _get(r, "sha256"):
            problems.append(f"Piece {i + 1} is different from the one this review is about.")
        elif not _same(utf16_len(texts[i]), _get(r, "chars")):
            problems.append(f"Piece {i + 1}'s length in the receipt (chars) is wrong.")
        name = g.get("name")
        if not _same("" if name is None else name, _get(r, "name")):
            problems.append(f"Piece {i + 1}'s name differs from the one in the receipt (names are part of pieces_sha256).")


def verify_review_outcome(
    receipt: Any,
    task: Optional[str] = None,
    output: Optional[str] = None,
    tier: Optional[str] = None,
    panel: Any = None,
    funding: Optional[str] = None,
    pieces: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Check a sealed-review receipt offline (not the signature: use :func:`verify_signature` for that).

    For a multi-part review (``review-3x2``, ``review-3x3``; ``receipt_format: "review-multi-v1"``) pass ``pieces``
    (the list of ``{"name"?, "text"}`` you hold, in order) instead of ``output``.

    Confirms it's a review (never a match receipt), that it's about exactly the ``task`` and ``output`` you pass
    (by SHA-256 of the exact text), that the reviewers are the ones you asked for (``panel``), that every vote
    comes from the committed reviewer, and that the verdict and tally recompute from the votes. Pass
    ``funding="free"`` to also require a free review's ``"funding": "free"``.

    Returns ``{"ok": bool, "problems": [...], "trusts": [...]}``.
    """
    p = _payload(receipt)
    problems: List[str] = []
    trusts = [
        "Each reviewer's verdict is that AI model's judgment: auditable (model, prompt fingerprint), not "
        "recomputable, and not a guarantee the output is correct."
    ]
    if not _same(_get(p, "type"), "review"):
        return {"ok": False, "problems": ["Not a sealed-review receipt."], "trusts": trusts}
    fp = _format_problem(p, "review")
    if fp:
        return {"ok": False, "problems": [fp], "trusts": trusts}
    t = _get(p, "tier")
    multi = _same(_get(p, "receipt_format"), MULTI_FORMAT)
    is_multi_tier = isinstance(t, str) and t in MULTI_PIECES
    if multi and not is_multi_tier:
        return {"ok": False, "problems": [
            f"A {MULTI_FORMAT} receipt must be for a multi-part tier (review-3x2 or review-3x3), not {_s(t)[:40]}."], "trusts": trusts}
    if is_multi_tier and not multi:
        return {"ok": False, "problems": [
            f"A {t} receipt must say receipt_format {MULTI_FORMAT}; this one doesn't, so it can't be checked."], "trusts": trusts}
    if not isinstance(t, str) or t not in _REVIEW_SIZE:
        return {"ok": False, "problems": [
            f"This client doesn't know the {_s(_get(p, 'tier'))[:40]} review tier, so nothing was confirmed. Update the "
            "client (pip install -U agent-referee-client)."], "trusts": trusts}
    if tier and not _same(_get(p, "tier"), tier):
        problems.append(f"This review is {_s(_get(p, 'tier'))}, not {tier}.")
    if multi:
        _check_pieces(p, output, pieces, problems)
    elif pieces is not None:
        problems.append("This is a single-piece review: check it with output, not pieces.")
    if not multi and output is not None and not _same(sha256_hex(output), _get(p, "output_sha256")):
        problems.append("This review is about a different output than the one you have.")
    if task is not None and not _same(sha256_hex(task), _get(p, "task_sha256")):
        problems.append("This review is for a different task than the one you have.")
    reviewers = _get(p, "reviewers")
    models = _nn(_get(reviewers, "models"), [])
    models = models if isinstance(models, list) else []
    pp = panel_problem(panel, models, _get(reviewers, "panel_selection"))
    if pp:
        problems.append(f"The reviewers differ from what you asked for: {pp}.")
    votes = _nn(_get(p, "votes"), [])
    votes = votes if isinstance(votes, list) else []
    size = _REVIEW_SIZE.get(_get(p, "tier")) if isinstance(_get(p, "tier"), str) else None
    if not size or len(models) != size or len(votes) != size:
        problems.append(f"Expected {size or '?'} reviewer votes, the receipt has {len(votes)}.")
    criteria = _get(p, "criteria")
    for i, v in enumerate(votes):
        if not _same(_get(v, "model"), _get(models, i)):
            problems.append(f"Vote {i + 1} doesn't come from the committed reviewer ({_s(_get(models, i))}).")
        if _get(v, "verdict") not in REVIEW_VERDICTS:
            problems.append(f"Vote {i + 1} has an unknown verdict.")
        scores = _get(v, "scores")
        if isinstance(criteria, list) and criteria and _truthy(scores):
            total = _weighted_total(scores, criteria)
            if not _same(_get(v, "total"), total):
                problems.append(f"Vote {i + 1}'s weighted total doesn't match its scores.")
    if multi:
        if not _same(_get(reviewers, "decision_rule"), "majority"):
            problems.append("A multi-part review is decided by majority; this receipt names another rule.")
        if len({_company(m) for m in models}) != len(models):
            problems.append("The reviewers aren't from three different AI companies.")
        for i, v in enumerate(votes):
            d = _get(v, "issues_dropped")
            if not (_is_int(d) and d >= 0):
                problems.append(f"Vote {i + 1} is missing issues_dropped (always present in a multi-part receipt).")
        if _same(_get(p, "funding"), "free"):
            problems.append("Multi-part reviews are paid only, but this receipt says it was free.")
        if votes:
            first = _get(votes[0], "verdict")
            if not _same(_get(_get(p, "result"), "unanimous"), all(_same(_get(v, "verdict"), first) for v in votes)):
                problems.append("The receipt's unanimous flag doesn't match the votes.")
    tally = {k: 0 for k in REVIEW_VERDICTS}
    for v in votes:
        verdict = _get(v, "verdict")
        if isinstance(verdict, str) and verdict in tally:
            tally[verdict] += 1
    rule = _get(reviewers, "decision_rule")
    expected: Any = UNDEF
    if _same(rule, "single"):
        expected = _get(_get(votes, 0), "verdict")
    elif _same(rule, "majority"):
        expected = next((k for k in REVIEW_VERDICTS if tally[k] > len(votes) / 2), "split")
    else:
        problems.append(f'Unknown decision rule "{_s(rule)}".')
    result = _get(p, "result")
    if expected is not UNDEF and not _same(_get(result, "verdict"), expected):
        problems.append(f"By the {_s(rule)} rule the verdict should be {_s(expected)}.")
    if _canon(_nn(_get(result, "tally"), {})) != _canon(tally):
        problems.append("The vote tally doesn't match the votes.")
    if funding == "free" and not _same(_get(p, "funding"), "free"):
        problems.append('A free review\'s receipt should say "funding": "free".')
    if funding == "free" and _get(p, "payer") is not None:
        problems.append("A free review's receipt shouldn't name a payer.")
    return {"ok": not problems, "problems": problems, "trusts": trusts}


# ---------------------------------------------------------------------------------------------------------------
# Matches
# ---------------------------------------------------------------------------------------------------------------


def settings_of(x: Dict[str, Any]) -> Dict[str, Any]:
    """The settings that decide a match, in a comparable form (what an agent agreed to)."""
    out: Dict[str, Any] = {
        "tier": _get(x, "tier"),
        "seats": _jsort(_nn(_get(x, "seats"), [])),
        "weights": _get(x, "weights"),
        "options": _get(x, "options"),
        "rules": _get(x, "rules"),
        "demo": _truthy(_get(x, "demo")),
    }
    for k in ("criteria", "host_pays", "panel"):
        if _truthy(_get(x, k)):
            out[k] = x[k]
    return out


def _check_settings(p: Any, req: Dict[str, Any], problems: List[str]) -> None:
    def flag(what: str) -> None:
        problems.append(f"The receipt's {what} differs from what you agreed to.")

    if not _same(_get(p, "tier"), _get(req, "tier")):
        flag("tier")
    parties = _nn(_get(p, "parties"), [])
    if _canon(_jsort(parties if isinstance(parties, list) else [])) != _canon(_get(req, "seats")):
        flag("seats")
    if _truthy(_get(p, "demo")) != _truthy(_get(req, "demo")):
        flag("demo setting")
    if not _same(_nn(_get(req, "host_pays"), None), _nn(_get(p, "host_pays"), None)):
        flag("payer (host_pays)")
    out = _nn(_get(p, "outcome"), {})
    if _truthy(_get(out, "weights")) and _truthy(_get(req, "weights")) and _canon(out["weights"]) != _canon(req["weights"]):
        flag("odds (weights)")
    if _truthy(_get(out, "options")) and _truthy(_get(req, "options")) and _canon(out["options"]) != _canon(req["options"]):
        flag("options")
    proof = _get(p, "proof")
    rh = _get(proof, "rules_sha256")
    if _truthy(rh) and _truthy(_get(req, "rules")) and not _same(rh, sha256_hex(req["rules"])):
        flag("rules")
    judge = _get(proof, "judge")
    if _truthy(_get(req, "panel")) and _truthy(judge):
        panel = _nn(_get(judge, "panel"), [])
        models = [_get(s, "model") for s in panel] if isinstance(panel, list) else []
        problem = panel_problem(req["panel"], models, _get(judge, "panel_selection"))
        if problem:
            problems.append(f"The receipt's judges differ from what you agreed to: {problem}.")
    if _get(p, "tier") in JUDGE_TIERS and not _truthy(_get(out, "by_forfeit")):
        ch = _get(proof, "criteria_sha256")
        criteria = _get(req, "criteria")
        if isinstance(criteria, list) and criteria:
            bad = not _same(ch, sha256_hex(canonical_json(criteria)))
        else:
            bad = ch is not UNDEF
        if bad:
            flag("scoring criteria")


def _weighted_total(scores: Any, criteria: List[Any]) -> Any:
    """Sum of weight × score over the criteria (a missing score counts 0). NaN if the data isn't numeric."""
    try:
        total = 0
        for c in criteria:
            w, x = _get(c, "weight"), _nn(_get(scores, _get(c, "name")), 0)
            if isinstance(w, bool) or isinstance(x, bool) or not isinstance(w, (int, float)) or not isinstance(x, (int, float)):
                return float("nan")
            total += w * x
        return total
    except TypeError:
        return float("nan")


def _criteria_pick(scores: Any, criteria: List[Dict[str, Any]]) -> Dict[str, Any]:
    totals: Dict[str, Any] = {}
    for party, s in (_nn(scores, {}) or {}).items():
        totals[party] = _weighted_total(s, criteria)
    ranked = sorted(totals.items(), key=lambda kv: -kv[1])
    pick = ranked[0][0] if len(ranked) >= 2 and ranked[0][1] != ranked[1][1] else None
    return {"totals": totals, "pick": pick}


def _piece_text(x: Any) -> Any:
    return x if isinstance(x, str) else x.get("text") if isinstance(x, dict) else None


def _check_judge_pieces(p: Any, given: List[Any], problems: List[str]) -> None:
    """The multi-part part of a ``judge-multi-v1`` check (see the JS client's checkJudgePieces, same messages).
    ``given`` is a list of (seat, texts) pairs: the pieces passed in, plus the seat's own (both are checked)."""
    n = JUDGE_MULTI_PIECES[_get(p, "tier")]
    proof = _nn(_get(p, "proof"), {})
    out = _nn(_get(p, "outcome"), {})
    judge = _nn(_get(proof, "judge"), {})
    if isinstance(proof, dict) and "submission_sha256" in proof:
        problems.append("A multi-part judge receipt never has proof.submission_sha256; this one does.")
    if not _same(_get(judge, "format_version"), JUDGE_MULTI_FORMAT_VERSION):
        problems.append(f"The judges' instructions should be {JUDGE_MULTI_FORMAT_VERSION}; this receipt says {_s(_get(judge, 'format_version'))[:60]}.")
    parties = _jsort(x for x in _nn(_get(p, "parties"), []) if isinstance(x, str))
    forfeited = _nn(_get(out, "forfeited"), [])
    forfeited = forfeited if isinstance(forfeited, list) else []
    by_forfeit = _truthy(_get(out, "by_forfeit"))
    expect_sides = [s for s in parties if s not in forfeited] if by_forfeit else parties
    pieces = _get(proof, "pieces")
    pieces = pieces if isinstance(pieces, dict) else {}
    hashes = _get(proof, "pieces_sha256")
    hashes = hashes if isinstance(hashes, dict) else {}
    sides = _jsort(pieces.keys())
    if sides != expect_sides:
        problems.append(
            "A forfeit receipt must show the pieces of the side that submitted, and only those." if by_forfeit
            else "A judged receipt must have both sides' pieces."
        )
    if _jsort(hashes.keys()) != sides:
        problems.append("pieces_sha256 must name exactly the sides that have pieces.")
    if by_forfeit:
        # The service only signs a forfeit for a two-party match where one side submitted: that side wins, and the
        # other is listed as forfeiting. The pieces show who submitted, so the winner and the forfeit list must match.
        submitted = sides[0] if len(sides) == 1 else None
        others = [s for s in parties if s != submitted]
        if submitted is None or not _same(_get(out, "winner"), submitted) or _jsort(x for x in forfeited if isinstance(x, str)) != others or len(forfeited) != len(others):
            problems.append("In a forfeit, the side that submitted must be the winner, and only the other side may be listed as forfeiting.")
    for side in sides:
        recs = pieces[side]
        if not isinstance(recs, list) or len(recs) != n:
            problems.append(f"{_s(side)} should have {n} pieces on {_s(_get(p, 'tier'))}; the receipt lists {len(recs) if isinstance(recs, list) else 'none'}.")
            continue
        for i, r in enumerate(recs):
            ok = (
                isinstance(r, dict)
                and sorted(r.keys()) == ["chars", "index", "sha256"]
                and _same(_get(r, "index"), i + 1)
                and isinstance(_get(r, "sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", _get(r, "sha256")) is not None
                and _is_int(_get(r, "chars"))
                and _get(r, "chars") > 0
            )
            if not ok:
                problems.append(f"{_s(side)}'s piece {i + 1} record in the receipt is malformed.")
        if not _same(sha256_hex(_canon(recs)), _get(hashes, side)):
            problems.append(f"{_s(side)}'s pieces_sha256 doesn't match its list of pieces.")
    if not by_forfeit:
        panel = _nn(_get(judge, "panel"), [])
        models = [_s(_get(x, "model")) for x in panel] if isinstance(panel, list) else []
        if len(models) != 3 or len({_company(m) for m in models}) != 3:
            problems.append("The judges aren't three, from three different AI companies.")
    for side, texts0 in given:
        recs = pieces.get(side)
        texts = [_piece_text(x) for x in texts0] if isinstance(texts0, (list, tuple)) else None
        if not isinstance(recs, list):
            problems.append(f"The receipt has no pieces for {_s(side)}.")
            continue
        if texts is None or len(texts) != n:
            problems.append(f"{_s(side)} has {n} pieces on {_s(_get(p, 'tier'))}; you passed {len(texts) if texts is not None else 'something that is not a list'}.")
            continue
        if any(not isinstance(x, str) for x in texts):
            problems.append(f"A piece you passed for {_s(side)} has no text.")
            continue
        hs = [sha256_hex(x) for x in texts]
        rec_hs = [_s(_get(r, "sha256")) for r in recs]
        if sorted(hs) == sorted(rec_hs) and hs != rec_hs:
            problems.append(f"These are {_s(side)}'s pieces, but in a different order.")
            continue
        for i, h in enumerate(hs):
            if h != _get(recs[i], "sha256"):
                problems.append(f"{_s(side)}'s piece {i + 1} is different from the one in the receipt.")
            elif not _same(utf16_len(texts[i]), _get(recs[i], "chars")):
                problems.append(f"{_s(side)}'s piece {i + 1} length in the receipt (chars) is wrong.")


def _check_judge_decision(p: Any, problems: List[str], trusts: List[str]) -> None:
    out = _nn(_get(p, "outcome"), {})
    judge = _nn(_get(_get(p, "proof"), "judge"), {})
    criteria = _get(out, "criteria")
    has_criteria = isinstance(criteria, list) and len(criteria) > 0
    if _truthy(_get(out, "mistrial")):
        code = _get(out, "mistrial_code")
        trusts.append(
            f"Mistrial{f' ({_s(code)})' if _truthy(code) else ''}: "
            f"{_s(_nn(_get(out, 'mistrial_reason'), 'no fair verdict was possible'))}. The dispute goes back to the owners."
        )
    if _same(_get(p, "tier"), "judge"):
        trusts.append("The verdict is the AI judge's decision; it can be audited (rules, model, prompt fingerprint) but not recomputed.")
        if has_criteria and _truthy(_get(out, "scores")):
            cp = _criteria_pick(out["scores"], criteria)
            if _canon(cp["totals"]) != _canon(_nn(_get(out, "totals"), {})):
                problems.append("The criteria totals don't match the judge's scores and the agreed weights.")
            if not _truthy(_get(out, "mistrial")) and not _same(_nn(_get(out, "winner"), None), cp["pick"]):
                problems.append(f"By the scores and agreed weights the winner should be {cp['pick'] or 'nobody (a tie)'}.")
        return
    trusts.append(
        "Each panelist's vote is that AI judge's decision (auditable, not recomputable). How the votes combine was "
        "recomputed from the receipt."
    )
    votes = _nn(_get(out, "votes"), [])
    panel = _nn(_get(judge, "panel"), [])
    if not isinstance(votes, list) or not isinstance(panel, list) or len(votes) != len(panel) or not panel:
        n = len(votes) if isinstance(votes, list) else 0
        problems.append(f"Expected {len(panel) if isinstance(panel, list) and panel else 'a panel of'} votes, the receipt has {n}.")
        return
    picks: List[Any] = []
    for i, v in enumerate(votes):
        if not _same(_get(v, "model"), _get(panel[i], "model")) or not _same(_get(v, "order"), _get(panel[i], "order")):
            problems.append(f"Vote {i + 1} doesn't come from the committed panelist ({_s(_get(panel[i], 'model'))}).")
        w = _get(v, "winner")
        if w is None:
            picks.append(None)
        elif has_criteria and _truthy(_get(v, "scores")):
            picks.append(_criteria_pick(v["scores"], criteria)["pick"])
        else:
            picks.append(w)
    parties = _jsort(_nn(_get(p, "parties"), []))
    tally: Dict[str, Any] = {k: 0 for k in parties + ["mistrial"]}
    for x in picks:
        k = "mistrial" if x is None or x is UNDEF else _s(x)
        tally[k] = tally[k] + 1 if k in tally else float("nan")  # JavaScript: undefined + 1 is NaN
    rule = _get(judge, "decision_rule")
    winner: Any = None
    if _same(rule, "unanimous"):
        winner = picks[0] if _truthy(picks[0]) and all(_same(x, picks[0]) for x in picks) else None
    elif _same(rule, "majority"):
        winner = next((s for s in parties if isinstance(tally.get(s), int) and tally[s] > len(picks) / 2), None)
    else:
        problems.append(f'Unknown decision rule "{_s(rule)}".')
    if not _same(_nn(_get(out, "winner"), None), winner):
        problems.append(f"By the {_s(rule)} rule the panel's decision should be {_s(winner) if winner is not None else 'a mistrial'}.")
    if _truthy(_get(out, "tally")) and _canon(out["tally"]) != _canon(tally):
        problems.append("The vote tally doesn't match the votes.")


def _rounds_of(proof: Any) -> List[Any]:
    rounds = _get(proof, "rounds")
    if rounds is UNDEF or rounds is None:
        return [{"round": 1, "commits": _get(proof, "commits"), "reveals": _get(proof, "reveals")}]
    return rounds if isinstance(rounds, list) else []


def find_contradictions(receipt: Any, acks: Iterable[Any] = ()) -> List[Dict[str, Any]]:
    """Every signed step acknowledgment that the receipt contradicts, as ``[{"reason", "ack"}]``.

    A commitment or reveal that changed or vanished, a forfeit for a step the service had accepted, or a changed
    submission or paying wallet. Both documents are signed by the service, so anyone can check this: verify both
    signatures with :func:`verify_signature`, then run this. No seed or secret needed.
    """
    p = _payload(receipt)
    out: List[Dict[str, Any]] = []
    if not _truthy(_get(p, "match_id")):
        return out
    proof = _nn(_get(p, "proof"), {})
    forfeited = list(_nn(_get(_get(p, "outcome"), "forfeited"), []))
    rounds = _rounds_of(proof)
    explicit_rounds = _get(proof, "rounds") is not UNDEF and _get(proof, "rounds") is not None

    def round_for(n: Any) -> Any:
        for r in rounds:
            if _same(_get(r, "round"), n):
                return r
        return rounds[0] if len(rounds) == 1 and not explicit_rounds else UNDEF

    for ack in acks or []:
        a = _get(ack, "payload")
        if not _same(_get(a, "type"), "step-ack") or not _same(_get(a, "match_id"), p["match_id"]):
            continue
        seat = _get(a, "seat")
        step = _get(a, "step")

        def flag(reason: str, ack: Any = ack, step: Any = step) -> None:
            out.append({"reason": f"Signed {_s(step)} acknowledgment contradicts the receipt: {reason}", "ack": ack})

        if _same(step, "commit") or _same(step, "reveal"):
            r = round_for(_get(a, "round"))
            c = _get(_get(r, "commits"), seat)
            if not _same(c, _get(a, "commitment")):
                flag(f"{_s(seat)}'s round {_s(_get(a, 'round'))} commitment {'was changed' if _truthy(c) else 'is missing'}.")
            if _same(step, "reveal"):
                v = _get(_get(r, "reveals"), seat)
                if not _truthy(v) or not _same(_get(v, "value"), _get(a, "value")) or not _same(_get(v, "nonce"), _get(a, "nonce")):
                    flag(f"{_s(seat)}'s round {_s(_get(a, 'round'))} reveal {'was changed' if _truthy(v) else 'is missing'}.")
                if seat in forfeited:
                    flag(f"{_s(seat)} is listed as forfeiting after the service accepted its reveal.")
        elif _same(step, "submit"):
            if seat in forfeited:
                flag(f"{_s(seat)} is listed as forfeiting after the service accepted its submission.")
            if _same(_get(a, "receipt_format"), JUDGE_FORMAT) or _same(_get(p, "receipt_format"), JUDGE_FORMAT):
                # Multi-part judge tiers: the ack names the side's pieces; the receipt must show exactly those.
                recs = _get(_get(proof, "pieces"), seat)
                if recs is UNDEF or recs is None:
                    flag(f"{_s(seat)}'s accepted pieces are missing from the receipt.")
                elif _canon(recs) != _canon(_nn(_get(a, "pieces"), None)) or not _same(_get(_get(proof, "pieces_sha256"), seat), _get(a, "pieces_sha256")):
                    flag(f"{_s(seat)}'s submission was changed.")
            else:
                h = _get(_get(proof, "submission_sha256"), seat)
                if _truthy(h) and not _same(h, _get(a, "position_sha256")):
                    flag(f"{_s(seat)}'s submission was changed.")
        elif _same(step, "join"):
            paid = _get(_get(p, "paid_by"), seat)
            if _truthy(paid) and _truthy(_get(a, "paid_by")) and not _same(paid, a["paid_by"]):
                flag(f"{_s(seat)}'s paying wallet was changed.")
    return out


def receipt_terms(doc: Any, expected: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The Terms a receipt records, if the service signed them in (``terms_version``, ``terms_sha256``), or None for
    receipts without them. With ``expected`` (e.g. ``"v2026-11-01"``), ``matches`` says whether it's that version.

    Returns ``{"version": str, "sha256": str | None, "matches": bool | None}`` or None.
    """
    p = _payload(doc) if isinstance(doc, dict) else {}
    if not isinstance(p, dict) or not isinstance(p.get("terms_version"), str):
        return None
    sha = p.get("terms_sha256")
    sha = sha if isinstance(sha, str) and re.fullmatch(r"[0-9a-f]{64}", sha) else None
    return {"version": p["terms_version"], "sha256": sha, "matches": None if expected is None else p["terms_version"] == expected}


def judge_advisory(doc: Any) -> Dict[str, Any]:
    """The advisory note on a judge-tier verdict, read from a receipt, its payload or a match status: the service's own
    sentence (``advisory``) and its human-review marker (``human_review``, ``"required"`` on every judge tier). Both
    are in the signed part of a judge receipt. Show ``advisory`` wherever you show a verdict, and hold the verdict for
    a person when ``human_review_required`` is true. Luck tiers and reviews have neither.

    Returns ``{"advisory": str | None, "human_review": str | None, "human_review_required": bool}``.
    """
    p = _payload(doc) if isinstance(doc, dict) else {}
    if not isinstance(p, dict):
        p = {}
    a = p.get("advisory")
    advisory = a if isinstance(a, str) and a.strip() else None
    h = p.get("human_review")
    human_review = h if isinstance(h, str) else None
    return {"advisory": advisory, "human_review": human_review, "human_review_required": human_review == "required"}


def verify_outcome(
    receipt: Any, seat: Any = None, acks: Optional[Iterable[Any]] = None, pieces: Optional[Dict[str, List[Any]]] = None
) -> Dict[str, Any]:
    """Re-derive a match receipt's result yourself, without trusting the service's arithmetic. No network.

    Checks every reveal against its commitment, then recomputes the winner (or picked option) the way the service
    does. Pass ``seat`` (the :class:`Seat` you played with) so it also confirms the receipt holds exactly what you
    committed and revealed, that it describes the match you agreed to, and that none of the service's signed step
    acknowledgments contradict it. Doesn't check the signature: use :func:`verify_signature` too.

    For a multi-part judge match (``judge-3x2``, ``judge-3x3``; ``receipt_format: "judge-multi-v1"``) pass ``pieces``,
    ``{seat: [text, ...]}`` for the sides whose pieces you hold, to confirm the receipt holds exactly them. A seat that
    submitted pieces checks its own.

    Returns ``{"ok", "recomputed", "problems", "trusts", "evidence", "advisory", "human_review"}``. On judge tiers
    ``advisory`` is the receipt's own signed sentence (show it with the verdict) and ``human_review`` is ``"required"``;
    both are also listed in ``trusts``. Other tiers have ``None``.
    """
    p = _payload(receipt)
    problems: List[str] = []
    trusts: List[str] = []
    if not _truthy(_get(p, "tier")) or not _truthy(_get(p, "outcome")):
        return {"ok": False, "recomputed": False, "problems": ["Not a receipt payload."], "trusts": trusts, "evidence": []}
    fp = _format_problem(p)
    if fp:
        return {"ok": False, "recomputed": False, "problems": [fp], "trusts": trusts, "evidence": []}
    multi_judge = _same(_get(p, "receipt_format"), JUDGE_FORMAT)
    t = _get(p, "tier")
    is_mj_tier = isinstance(t, str) and t in JUDGE_MULTI_PIECES
    if multi_judge and not is_mj_tier:
        return {"ok": False, "recomputed": False, "problems": [
            f"A {JUDGE_FORMAT} receipt must be for a multi-part judge tier (judge-3x2 or judge-3x3), not {_s(t)[:40]}."], "trusts": trusts, "evidence": []}
    if is_mj_tier and not multi_judge:
        return {"ok": False, "recomputed": False, "problems": [
            f"A {t} receipt must say receipt_format {JUDGE_FORMAT}; this one doesn't, so it can't be checked."], "trusts": trusts, "evidence": []}
    out = p["outcome"]
    proof = _nn(_get(p, "proof"), {})

    me = seat
    my_acks = acks if acks is not None else (getattr(me, "acks", None) or [])
    evidence = find_contradictions(p, my_acks)
    if me is None:
        problems.extend(e["reason"] for e in evidence)

    adv = judge_advisory(p)
    if adv["advisory"]:
        trusts.append(adv["advisory"])  # the receipt's own words, signed (judge spec §9): not a copy kept here
    if adv["human_review_required"]:
        trusts.append(
            "The service marks this verdict for a person's review (human_review: required). Don't act on it in a step "
            "that can't be undone until someone has reviewed it.")

    def done(recomputed: bool) -> Dict[str, Any]:
        return {"ok": not problems, "recomputed": recomputed, "problems": problems, "trusts": trusts, "evidence": evidence,
                "advisory": adv["advisory"], "human_review": adv["human_review"]}

    requested = getattr(me, "requested", None) if me is not None else None
    if requested:
        _check_settings(p, requested, problems)

    if me is not None:
        if not _same(getattr(me, "match_id", UNDEF), _get(p, "match_id")):
            problems.append("This receipt is for a different match than your seat.")
        rounds = _rounds_of(proof)
        my_seat = getattr(me, "seat", None)
        for m in getattr(me, "mine", None) or []:
            want = _nn(_get(m, "round"), 1)
            r = next((x for x in rounds if _same(_get(x, "round"), want)), UNDEF)
            if r is UNDEF and len(rounds) == 1:
                r = rounds[0]
            if r is UNDEF:
                problems.append(f"Round {_s(_get(m, 'round'))}: missing from the receipt.")
                continue
            c = _get(_get(r, "commits"), my_seat)
            v = _get(_get(r, "reveals"), my_seat)
            if c is not UNDEF and not _same(c, _get(m, "commitment")):
                problems.append(f"Round {_s(_get(m, 'round'))}: your commitment was replaced.")
            if _truthy(v) and (not _same(_get(v, "value"), _get(m, "value")) or not _same(_get(v, "nonce"), _get(m, "nonce"))):
                problems.append(f"Round {_s(_get(m, 'round'))}: your reveal was replaced.")
            if c is UNDEF and not _truthy(_get(out, "by_forfeit")):
                problems.append(f"Round {_s(_get(m, 'round'))}: your commitment is missing.")
        problems.extend(e["reason"] for e in evidence)
        if problems:
            problems.append(
                "Don't accept this result. Keep this signed receipt and seat.mine (your seed and nonce): the receipt is "
                "the service's signed claim, and your inputs show it differs from what you sent."
            )

    def check_reveals(commits: Any, reveals: Any, label: str = "") -> None:
        commits = _nn(commits, {})
        for s, r in (_nn(reveals, {}) or {}).items():
            c = _get(commits, s)
            if not _truthy(c):
                problems.append(f"{label}{s} revealed without a commitment.")
            elif not _same(sha256_hex(f"{_s(_get(r, 'value'))}:{_s(_get(r, 'nonce'))}"), c):
                problems.append(f"{label}{s}'s reveal doesn't match its commitment.")

    def combined(seeds: Dict[str, Any]) -> int:
        parts = [f"{s}={_s(seeds[s])}" for s in _jsort(seeds)]
        h = sha256_hex(f"{_s(p.get('match_id', UNDEF))}|1|" + "|".join(parts))
        cs = _get(proof, "combined_sha256")
        if _truthy(cs) and not _same(cs, h):
            problems.append("Combined random value doesn't match the reveals.")
        return int(h[:16], 16)

    forfeited = _get(out, "forfeited")
    if _truthy(_get(out, "by_forfeit")) or (isinstance(forfeited, list) and forfeited):
        who = ", ".join(_s(x) for x in _nn(forfeited, []))
        trusts.append(f"That {who} missed a deadline is the service's record; it can't be recomputed from the receipt.")

    tier = _get(p, "tier")
    if tier in JUDGE_TIERS:
        if multi_judge:
            given: List[Any] = []
            if pieces is not None:
                if not isinstance(pieces, dict):
                    problems.append('pieces must be by seat: { "seat": ["text 1", "text 2"] }.')
                else:
                    given.extend(pieces.items())
            mine = getattr(me, "pieces", None) if me is not None else None
            if mine:
                given.append((me.seat, mine))
            _check_judge_pieces(p, given, problems)
        if not _truthy(_get(out, "by_forfeit")):
            _check_judge_decision(p, problems, trusts)
        return done(False)

    if tier in ("rps", "rps-bo3"):
        parties = _nn(_get(p, "parties"), [])
        a, b = _get(parties, 0), _get(parties, 1)
        wins = {_s(a): 0, _s(b): 0}
        for r in _nn(_get(proof, "rounds"), []):
            check_reveals(_get(r, "commits"), _get(r, "reveals"), f"Round {_s(_get(r, 'round'))}: ")
            ra, rb = _get(_get(r, "reveals"), a), _get(_get(r, "reveals"), b)
            if _truthy(ra) and _truthy(rb):
                va, vb = _get(ra, "value"), _get(rb, "value")
                expected = None if _same(va, vb) else a if _same(_RPS_BEATS.get(va, UNDEF) if isinstance(va, str) else UNDEF, vb) else b
                if not _same(_nn(_get(r, "winner"), None), expected):
                    problems.append(f"Round {_s(_get(r, 'round'))}: winner should be {_s(expected) if expected is not None else 'a tie'}.")
                if _truthy(expected):
                    wins[_s(expected)] += 1
        if not _truthy(_get(out, "by_forfeit")):
            target = 1 if tier == "rps" else 2
            champ = next((s for s in (a, b) if wins[_s(s)] >= target), None)
            if not _same(_nn(_get(out, "winner"), None), champ):
                problems.append(f"Winner should be {_s(champ) if champ is not None else 'nobody'}.")
        return done(not _truthy(_get(out, "by_forfeit")))

    check_reveals(_get(proof, "commits"), _get(proof, "reveals"))
    seeds = {s: _get(r, "value") for s, r in (_nn(_get(proof, "reveals"), {}) or {}).items()}

    if tier == "weighted":
        if _truthy(_get(out, "by_forfeit")):
            return done(False)
        weights = _nn(_get(out, "weights"), {})
        seats = _jsort(weights)
        for s in seats:
            if not _truthy(seeds.get(s)):
                problems.append(f"{s} has no reveal.")
        if not seats or not all(isinstance(weights[s], int) and not isinstance(weights[s], bool) for s in seats) or sum(weights[s] for s in seats) <= 0:
            problems.append("The receipt's odds (weights) aren't positive whole numbers.")
            return done(True)
        total = sum(weights[s] for s in seats)
        ticket = combined(seeds) % total
        winner = seats[-1]
        for s in seats:
            if ticket < weights[s]:
                winner = s
                break
            ticket -= weights[s]
        if not _same(_get(out, "winner"), winner):
            problems.append(f"Winner should be {winner}.")
        return done(True)

    if tier == "draw":
        options = _nn(_get(out, "options"), [])
        options = options if isinstance(options, list) else []
        idx = combined(seeds) % (len(options) or 1)
        if not _same(_get(out, "picked_index"), idx) or not _same(_get(out, "picked_option"), _get(options, idx)):
            problems.append(f"Picked option should be #{idx} ({_s(_get(options, idx))}).")
        return done(True)

    problems.append(f'Unknown tier "{_s(tier)}".')
    return done(False)


def verify_series(receipts: Iterable[Any]) -> Dict[str, Any]:
    """Check a rematch series from its receipts alone (any order; game 1 included or not).

    Confirms each game links to the previous receipt by hash, game numbers run in order, and every running tally
    adds up from the winners. Verify each receipt's signature too. Returns ``{"ok", "problems", "tally"}``.
    """
    ps = [x for x in (_payload(r) for r in (receipts or [])) if _truthy(x)]
    problems: List[str] = []
    if not ps:
        return {"ok": False, "problems": ["No receipts given."], "tally": None}
    fps = [fp for fp in (_format_problem(x) for x in ps) if fp]
    if fps:
        return {"ok": False, "problems": fps[:1], "tally": None}

    def game_of(p: Any) -> Any:
        return _nn(_get(_get(p, "series"), "game"), 1)

    ps.sort(key=game_of)
    series_id = _nn(_get(_get(ps[0], "series"), "series_id"), _get(ps[0], "match_id"))
    tally: Any = None
    for i, p in enumerate(ps):
        s = _get(p, "series")
        game = game_of(p)
        if game == 1:
            if not _same(_get(p, "match_id"), series_id):
                problems.append(f"Game 1 ({_s(_get(p, 'match_id'))}) isn't the start of series {_s(series_id)}.")
        elif not _same(_get(s, "series_id"), series_id):
            problems.append(f"Game {_s(game)} belongs to a different series.")
        prev = ps[i - 1] if i > 0 else None
        if prev is not None:
            if game_of(prev) != game - 1:
                problems.append(f"Game {_s(game - 1)} is missing.")
            else:
                if not _same(_get(s, "previous_match_id"), _get(prev, "match_id")):
                    problems.append(f"Game {_s(game)} doesn't follow game {_s(game - 1)}'s match.")
                if not _same(_get(s, "previous_receipt_sha256"), sha256_hex(canonical_json(prev))):
                    problems.append(f"Game {_s(game)} doesn't link to game {_s(game - 1)}'s receipt (it was changed or swapped).")
        parties = _nn(_get(p, "parties"), [])
        before = {"games": 0, "wins": {x: 0 for x in parties}, "no_result": 0} if game == 1 else tally
        if before:
            t = copy.deepcopy(before)
            t["games"] += 1
            w = _get(_get(p, "outcome"), "winner")
            if _truthy(w) and w in parties:
                t["wins"][w] = _nn(_get(t["wins"], w), 0) + 1
            else:
                t["no_result"] += 1
            if _truthy(_get(s, "tally")) and _canon(s["tally"]) != _canon(t):
                problems.append(f"Game {_s(game)}'s tally doesn't add up.")
            tally = t
        else:
            tally = _nn(_get(s, "tally"), None)  # series given from the middle: its signed tally is the start
    return {"ok": not problems, "problems": problems, "tally": tally}
