"""Paying over x402 (version 1, USDC on Base), and keeping payment proofs so a paid seat can be recovered.

Paying is optional. It needs Python 3.10+ and the official ``x402`` package::

    pip install "agent-referee-client[pay]"

This module never hand-signs anything: the EIP-3009 authorization is built and signed by ``x402``'s own v1
"exact" scheme. What it adds is a hard price cap that's checked before signing, and the exact ``X-PAYMENT``
header it produced, so a paid join can be recovered with that header if the reply is lost.
"""

from __future__ import annotations

import base64
import hashlib
import json
import errno
import os
import re
import sys
import time
from typing import Any, Dict, List, Optional

__all__ = ["X402Payer", "PaymentRefused", "FileProofStore", "MemoryProofStore", "default_proof_dir", "read_wallet_key"]

#: The service's fee wallet, if you want to refuse paying anyone else (``X402Payer(..., pay_to=FEE_WALLET)``).
FEE_WALLET = "0xF6B53AaCf822A36b7B2aF9C45BabE6E367ede4d3"

# A whole 0x + 64-hex value: not part of a longer hex string (such as a 130-hex signature).
_HEX64 = re.compile(r"(?<![0-9a-fA-Fx])0x[0-9a-fA-F]{64}(?![0-9a-fA-F])")


def read_wallet_key(path: str) -> str:
    """The private key in a wallet file: its single ``0x`` + 64-hex value, or the one on a line mentioning "key".

    The key is returned to the caller only, never printed. Mirrors ``scripts/wallet-key.mjs``.
    """
    with open(path, encoding="utf-8") as f:
        text = f.read()
    found = list(dict.fromkeys(m.lower() for m in _HEX64.findall(text)))
    if len(found) == 1:
        return found[0]
    if not found:
        raise ValueError("No 0x-prefixed 64-character key found in the wallet file.")
    on_key_lines = list(dict.fromkeys(m.lower() for line in text.splitlines() if "key" in line.lower() for m in _HEX64.findall(line)))
    if len(on_key_lines) == 1:
        return on_key_lines[0]
    raise ValueError(
        'The wallet file has several 64-character hex values and it isn\'t clear which is the private key. Use a file '
        'that holds only the key (or put it on a line with the word "key").'
    )


class PaymentRefused(Exception):
    """The payment wasn't made, so nothing was charged: the 402 offer was over the cap, on another network, to another
    recipient, or unreadable (checked before signing), or the signed result didn't match (checked after signing, and
    then never sent)."""


class X402Payer:
    """Signs x402 version-1 payments from one wallet, never above ``max_micro`` per request.

    ``max_micro`` is in USDC's smallest unit (1,000,000 = $1) and has no default, so no tier is blocked by a cap
    you didn't choose. Each price this client may pay: review 50,000; review-3 200,000; judge-3 350,000 per seat
    (700,000 as the host of an invite match).

    Use a dedicated wallet holding only a few dollars of USDC on Base.
    """

    def __init__(
        self,
        private_key: str,
        max_micro: int,
        network: str = "base",
        pay_to: Optional[str] = None,
        *,
        _scheme: Any = None,
    ) -> None:
        if not isinstance(max_micro, int) or isinstance(max_micro, bool) or max_micro <= 0:
            raise ValueError("max_micro must be a positive whole number of USDC micro-units (1000000 = $1).")
        if _scheme is None and (not isinstance(private_key, str) or not re.fullmatch(r"(0x)?[0-9a-fA-F]{64}", private_key.strip())):
            raise ValueError("private_key must be a 64-character hex key (optionally 0x-prefixed).")
        if sys.version_info < (3, 10):
            raise RuntimeError("Paying needs Python 3.10 or newer (the x402 package requires it). Free demos, free reviews and receipt checks work on 3.9.")
        try:
            from x402 import max_amount, x402ClientSync
            from x402.http import x402HTTPClientSync
        except ImportError as e:  # pragma: no cover - depends on the environment
            raise RuntimeError('Paying needs the x402 package: pip install "agent-referee-client[pay]"') from e
        if _scheme is None:
            try:
                from eth_account import Account
                from x402.mechanisms.evm.exact.v1.client import ExactEvmSchemeV1
            except ImportError as e:  # pragma: no cover - depends on the environment
                raise RuntimeError('Paying needs the x402 package with its EVM support: pip install "agent-referee-client[pay]"') from e
            key = private_key.strip()
            account = Account.from_key(key if key.startswith("0x") else "0x" + key)
            self.wallet: str = account.address
            scheme = ExactEvmSchemeV1(account)
        else:  # tests only: a stand-in for the signing scheme (no key is used, so there's none to validate)
            scheme = _scheme
            self.wallet = getattr(_scheme, "address", "")
        self.max_micro = max_micro
        self.network = network
        self.pay_to = pay_to.lower() if pay_to else None
        self._client = x402ClientSync()
        # Only x402 version 1 on this one network: that's what the service speaks.
        self._client.register_v1(network, scheme)
        self._client.register_policy(max_amount(max_micro))
        # x402's own default spend controls add a hidden $1 cap on top of max_micro. This payer enforces its own cap
        # (before and after signing), so lift that one but keep x402's allowlist of known assets (USDC). Not wrapped in
        # a try: if this x402 version can't do it, construction fails loudly instead of capping silently.
        self._client.set_spend_controls({"max_amount_per_payment": False})
        if self.pay_to:
            want = self.pay_to
            self._client.register_policy(lambda version, reqs: [r for r in reqs if str(r.pay_to).lower() == want])
        self._http = x402HTTPClientSync(self._client)
        self.last_settlement: Optional[Dict[str, Any]] = None

    @classmethod
    def from_wallet_file(cls, path: str, max_micro: int, **kw: Any) -> "X402Payer":
        """Read the key from a file (see :func:`read_wallet_key`). The key is never printed."""
        return cls(read_wallet_key(path), max_micro, **kw)

    def payment_header(self, payment_required: Any, headers: Dict[str, str]) -> str:
        """The signed ``X-PAYMENT`` header value for a 402 answer, or :class:`PaymentRefused` before signing."""
        offers = payment_required.get("accepts") if isinstance(payment_required, dict) else None
        asked = []
        for o in offers if isinstance(offers, list) else []:
            amount = o.get("maxAmountRequired") if isinstance(o, dict) else None
            if not isinstance(amount, str) or not re.fullmatch(r"\d+", amount):
                continue  # x402 v1 amounts are whole numbers written as strings
            asked.append((int(amount), o.get("network"), str(o.get("payTo", "")).lower()))
        if not asked:
            raise PaymentRefused("The service's 402 answer has no payment offer this client can read.")
        fits = [a for a in asked if a[0] <= self.max_micro and a[1] == self.network and (not self.pay_to or a[2] == self.pay_to)]
        if not fits:
            cheapest = min(a[0] for a in asked)
            if cheapest > self.max_micro:
                raise PaymentRefused(
                    f"The service asked for {cheapest / 1e6:.6g} USDC, more than this payer's cap of {self.max_micro / 1e6:.6g} USDC per request."
                )
            raise PaymentRefused(f"The service asked to be paid on a network or to a wallet this payer doesn't allow (network {self.network}).")
        try:
            lower = {k.lower(): v for k, v in (headers or {}).items()}
            pr = self._http.get_payment_required_response(lambda n: lower.get(n.lower()), payment_required)
            if getattr(pr, "x402_version", None) != 1:
                raise PaymentRefused("The service asked for an x402 version other than 1.")
            payload = self._client.create_payment_payload(pr)
            value = self._http.encode_payment_signature_header(payload).get("X-PAYMENT")
        except PaymentRefused:
            raise
        except Exception as e:
            raise PaymentRefused(f"Couldn't create the payment ({type(e).__name__}: {str(e)[:200]}).") from e
        if not value:
            raise PaymentRefused("The payment library didn't produce an X-PAYMENT header.")
        # Double-check what was signed against the offers that fit before signing, whatever the library did: the
        # amount, the network and the recipient must match one of them.
        try:
            signed = json.loads(base64.b64decode(value))
            auth = signed["payload"]["authorization"]
            amount, to = int(auth["value"]), str(auth["to"]).lower()
            if signed["network"] != self.network or amount > self.max_micro:
                raise ValueError
            if not any(amount <= a and to == r for a, _, r in fits):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError) as e:
            raise PaymentRefused("The signed payment didn't match the cap, network or recipient, so it wasn't sent.") from e
        return value

    def settled(self, headers: Dict[str, str]) -> Optional[Dict[str, Any]]:
        """Read the service's ``X-PAYMENT-RESPONSE`` (transaction hash, network, payer) after a paid request."""
        h = None
        try:
            lower = {str(k).lower(): v for k, v in (headers or {}).items()}
            h = lower.get("x-payment-response")
            if not h:
                return None
            data = json.loads(base64.b64decode(h))
            self.last_settlement = data if isinstance(data, dict) else {"raw": h}
        except Exception:
            self.last_settlement = {"raw": str(h)} if h else None
        return self.last_settlement


# ---------------------------------------------------------------------------------------------------------------
# Payment proofs (for recovering a paid seat)
# ---------------------------------------------------------------------------------------------------------------


def default_proof_dir() -> str:
    """The user's data folder for proofs: %LOCALAPPDATA%\\agent-referee\\proofs on Windows,
    ~/Library/Application Support/agent-referee/proofs on macOS, $XDG_DATA_HOME (or ~/.local/share)/agent-referee/proofs elsewhere."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "agent-referee", "proofs")


# Errors that mean "this filesystem can't open or sync a directory", not "the disk failed" (ledger 3.0-U01).
_DIR_SYNC_UNSUPPORTED = {e for e in (getattr(errno, n, None) for n in ("EINVAL", "ENOTSUP", "EOPNOTSUPP", "EBADF", "EACCES", "EPERM", "EISDIR", "ENOSYS")) if e is not None}


class FileProofStore:
    """One small JSON file per in-flight paid join, readable only by your user where the OS supports it.

    A proof is the signed, single-use payment header (not a private key), but until its match ends it can take
    over that seat, so treat it as sensitive: it's written before the paid request is sent and deleted as soon as
    the service gives a definite answer. Never log or commit these files.
    """

    def __init__(self, directory: Optional[str] = None) -> None:
        self.directory = directory or default_proof_dir()

    def _file(self, key: str) -> str:
        return os.path.join(self.directory, hashlib.sha256(key.encode("utf-8", "surrogatepass")).hexdigest()[:32] + ".json")

    def save(self, key: str, proof: str, info: Optional[Dict[str, Any]] = None) -> None:
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        try:
            os.chmod(self.directory, 0o700)
        except OSError:
            pass
        path = self._file(key)
        data = json.dumps({**(info or {}), "key": key, "proof": proof, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "saved_ts": time.time()})
        # Written to a temporary file, flushed, then renamed over the real one, so a crash mid-write never leaves a
        # half-written proof (or destroys an earlier one).
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            buf = data.encode("utf-8")
            while buf:
                buf = buf[os.write(fd, buf):]  # os.write may write less than asked
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        if os.name == "posix":  # make the new directory entry durable too, where the filesystem allows it
            # The proof itself is already written, flushed and in place. Some filesystems (network, FUSE, some
            # container mounts) can't open or sync a directory; that's not a reason to stop the paid request, so those
            # errors are skipped (ledger 3.0-U01). A real I/O error still raises.
            try:
                dfd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError as e:
                if e.errno not in _DIR_SYNC_UNSUPPORTED:
                    raise

    def remove(self, key: str) -> None:
        try:
            os.remove(self._file(key))
        except FileNotFoundError:
            pass

    def list(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            names = os.listdir(self.directory)
        except OSError:
            return out
        for n in names:
            if not n.endswith(".json"):
                continue
            try:
                with open(os.path.join(self.directory, n), encoding="utf-8") as f:
                    e = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(e, dict):
                out.append(e)
        return out


class MemoryProofStore:
    """Keeps proofs in memory only: covers a dropped connection, not a restart."""

    def __init__(self) -> None:
        self._d: Dict[str, Dict[str, Any]] = {}

    def save(self, key: str, proof: str, info: Optional[Dict[str, Any]] = None) -> None:
        self._d[key] = {**(info or {}), "key": key, "proof": proof, "saved_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "saved_ts": time.time()}

    def remove(self, key: str) -> None:
        self._d.pop(key, None)

    def list(self) -> List[Dict[str, Any]]:
        return [dict(e) for e in self._d.values()]

