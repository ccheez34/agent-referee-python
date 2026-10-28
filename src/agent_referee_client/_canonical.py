"""Byte-exact helpers shared with the service: canonical JSON, SHA-256, commitments and the joint random value.

The service signs ``canonicalJson(payload)`` from its TypeScript source. These functions reproduce it exactly:
object keys sorted by UTF-16 code units (JavaScript's default sort), strings escaped like ``JSON.stringify``, and
numbers written the way JavaScript writes them. Lengths the service checks are UTF-16 lengths (JavaScript
``.length``), so :func:`utf16_len` counts the same way.
"""

from __future__ import annotations

import hashlib
import math
import re
import secrets
from decimal import Decimal
from typing import Any, Dict, Mapping

__all__ = [
    "canonical_json",
    "sha256_hex",
    "utf16_len",
    "random_hex",
    "commitment",
    "combined_random",
    "weighted_pick",
    "draw_pick",
]

_SURROGATE = re.compile("[\ud800-\udfff]")


def _js_string_units(s: str) -> str:
    """Join surrogate pairs that Python holds as two code points into one, like JavaScript sees them.

    Lone surrogates stay as they are. Strings without surrogates are returned unchanged.
    """
    if not _SURROGATE.search(s):
        return s
    return s.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "surrogatepass")


def utf16_len(s: str) -> int:
    """Length in UTF-16 code units, like JavaScript's ``.length`` (an emoji counts as 2)."""
    return len(s.encode("utf-16-le", "surrogatepass")) // 2


def _utf16_key(s: str) -> bytes:
    # Big-endian UTF-16 bytes compare in the same order as UTF-16 code units.
    return _js_string_units(s).encode("utf-16-be", "surrogatepass")


_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _js_quote(s: str) -> str:
    """A string exactly as JavaScript's JSON.stringify writes it (non-ASCII kept as is, lone surrogates escaped)."""
    s = _js_string_units(s)
    out = ['"']
    for ch in s:
        e = _ESCAPES.get(ch)
        if e is not None:
            out.append(e)
        elif ch < " " or "\ud800" <= ch <= "\udfff":
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _js_number(x: Any) -> str:
    """A number exactly as JavaScript writes it (Number.prototype.toString, which JSON.stringify uses)."""
    if isinstance(x, int):
        if abs(x) <= 2**53:
            return str(x)
        try:
            x = float(x)  # JavaScript holds every number as a double, so a bigger integer is rounded the same way
        except OverflowError:
            return "null"  # too big for a double: JavaScript has Infinity, which JSON.stringify writes as null
    if not math.isfinite(x):
        return "null"  # JSON.stringify writes NaN and Infinity as null
    if x == 0:
        return "0"  # also -0
    sign = "-" if x < 0 else ""
    # repr gives the shortest digits that round-trip, which is what JavaScript prints too.
    t = Decimal(repr(abs(x))).as_tuple()
    digits = list(t.digits)
    exp = int(t.exponent)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
        exp += 1
    s = "".join(map(str, digits))
    k = len(s)
    n = exp + k  # the value is 0.s × 10^n
    if k <= n <= 21:
        body = s + "0" * (n - k)
    elif 0 < n <= 21:
        body = s[:n] + "." + s[n:]
    elif -6 < n <= 0:
        body = "0." + "0" * (-n) + s
    else:
        e = n - 1
        body = (s if k == 1 else s[0] + "." + s[1:]) + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + body


def canonical_json(value: Any) -> str:
    """The exact text the service signs: JSON with every object's keys sorted and no whitespace.

    Byte-identical to the service's ``canonicalJson`` for any JSON value (strings with emoji or lone surrogates,
    floats, big integers). ``None`` is written as ``null``, as JSON from the service has it.
    """
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _js_quote(value)
    if isinstance(value, (int, float)):
        return _js_number(value)
    if isinstance(value, (list, tuple)):
        return "[" + ",".join(canonical_json(v) for v in value) + "]"
    if isinstance(value, Mapping):
        items = {str(k): v for k, v in value.items()}
        keys = sorted(items, key=_utf16_key)
        return "{" + ",".join(_js_quote(k) + ":" + canonical_json(items[k]) for k in keys) + "}"
    raise TypeError(f"canonical_json can't encode {type(value).__name__}")


def _utf8(text: str) -> bytes:
    """UTF-8 bytes the way JavaScript's TextEncoder makes them (a lone surrogate becomes U+FFFD)."""
    if _SURROGATE.search(text):
        text = text.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    return text.encode("utf-8")


def sha256_hex(text: str) -> str:
    """SHA-256 of a string's UTF-8 bytes, as lowercase hex (how receipts fingerprint a task, output or receipt)."""
    return hashlib.sha256(_utf8(text)).hexdigest()


def random_hex(nbytes: int = 16) -> str:
    """Cryptographically random hex string (default 32 hex characters), used for seeds and nonces."""
    return secrets.token_hex(nbytes)


def commitment(value: str, nonce: str) -> str:
    """The commitment the service expects: ``sha256_hex(value + ":" + nonce)``."""
    return sha256_hex(f"{value}:{nonce}")


def combined_random(match_id: str, round_number: int, seeds: Mapping[str, str]) -> Dict[str, Any]:
    """The joint random value: ``sha256_hex(match_id|round|seat=seed|...)`` with seats sorted.

    Returns ``{"hex": ..., "value": int}``, where value is the first 16 hex digits as an integer.
    """
    parts = [f"{s}={seeds[s]}" for s in sorted(seeds, key=_utf16_key)]
    h = sha256_hex(f"{match_id}|{round_number}|" + "|".join(parts))
    return {"hex": h, "value": int(h[:16], 16)}


def weighted_pick(weights: Mapping[str, int], r: int) -> str:
    """The seat picked by the joint random value ``r``, walking the seats in sorted order (as the service does)."""
    seats = sorted(weights, key=_utf16_key)
    total = sum(weights[s] for s in seats)
    ticket = r % total
    for seat in seats:
        if ticket < weights[seat]:
            return seat
        ticket -= weights[seat]
    return seats[-1]


def draw_pick(options: list, r: int) -> int:
    """The index of the option picked by the joint random value ``r``."""
    return r % len(options)
