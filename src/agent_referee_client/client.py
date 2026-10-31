"""The Agent Referee client: matches between agents, sealed reviews, and seat recovery.

Synchronous, standard library only (plus ``cryptography`` for signature checks). Paying is optional: pass a payer
from :mod:`agent_referee_client.pay`. Mirrors the JavaScript client 0.10.0.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

from ._canonical import commitment as _commitment
from ._canonical import random_hex, utf16_len
from .verify import (
    PUBLIC_KEY,
    settings_of,
    verify_outcome,
    verify_review_outcome,
    verify_signature,
)

__all__ = ["AgentReferee", "Seat", "RefereeError", "DEFAULT_URL", "OFFICIAL_URLS", "STATUS_PAGE"]

__version__ = "0.4.0"

DEFAULT_URL = "https://agentreferee.ai"
#: Every address the official service answers on. An invite from any of them works with any of them.
OFFICIAL_URLS = ["https://agentreferee.ai", "https://agent-referee.fairplayclub.workers.dev"]
STATUS_PAGE = "https://stats.uptimerobot.com/dh6sVMvo3W"

JOIN_PATH = {
    "rps": "/join/rps",
    "rps-bo3": "/join/rps-bo3",
    "weighted": "/join/weighted",
    "judge": "/join/judge",
    "judge-2": "/join/judge-2",
    "judge-3": "/join/judge-3",
    "judge-3x2": "/join/judge-3x2",
    "judge-3x3": "/join/judge-3x3",
}
JUDGE_MULTI_PIECES = {"judge-3x2": 2, "judge-3x3": 3}
REVIEW_TIERS = ["review", "review-3", "review-3x2", "review-3x3"]
MULTI_PIECES = {"review-3x2": 2, "review-3x3": 3}
MOVES = ["rock", "paper", "scissors"]

log = logging.getLogger("agent_referee_client")

Transport = Callable[[str, str, Dict[str, str], Optional[bytes], float], Tuple[int, Dict[str, str], bytes]]


class RefereeError(Exception):
    """An answer from the service that isn't a success, or no answer at all (``status`` 0).

    ``message`` is the service's own wording where it gave one ("... Nothing was charged.", "... Your free
    review wasn't used."). ``body`` is its JSON reply (for example ``differences`` on a 409). ``retry_after`` is the
    number of seconds to wait when the service is busy (503), else None.
    """

    def __init__(self, message: str, status: int = 0, body: Any = None, retry_after: Optional[float] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body
        self.retry_after = retry_after

    def __repr__(self) -> str:
        return f"RefereeError({self.message!r}, status={self.status})"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect. urllib would otherwise resend the request's headers, X-PAYMENT included, to wherever
    the redirect points (and turn a POST into a GET). The 3xx comes back as the answer instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib_transport(method: str, url: str, headers: Dict[str, str], body: Optional[bytes], timeout: float):
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        try:
            data = e.read()
        except Exception:
            data = b""
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, data


def _clean(v: Any) -> Any:
    """Leave unused fields out: the service refuses null, so None values are dropped (in objects, at any depth)."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x is not None}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def _origin(url: str) -> str:
    """scheme://host[:port], lowercased, with default ports dropped and IPv6 hosts in brackets (like a browser's URL.origin)."""
    u = urllib.parse.urlsplit(url)
    scheme = (u.scheme or "").lower()
    host = (u.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = u.port
    except ValueError:
        raise RefereeError(f"That address has an invalid port: {url[:200]}", 400) from None
    if port is None or (scheme == "https" and port == 443) or (scheme == "http" and port == 80):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


class AgentReferee:
    """A client for https://agentreferee.ai.

    Args:
        url: Service address (default https://agentreferee.ai; the workers.dev address works too).
        payer: Something that can pay x402 requests, e.g. ``X402Payer(key, max_micro=200_000)``. Free calls never
            use it. Without one, paid joins and paid reviews fail with status 402 before anything is charged.
        public_keys: The service's signing key(s) to trust (default: the pinned official key). ``None`` fetches
            them from ``GET /pubkey`` instead, which also picks up a new key on its own but trusts that page.
        demo: Create free demo matches (no wallet, no payment). Receipts and acks are marked demo.
        poll_s: How often to poll while waiting (default 2 s).
        timeout_s: Longest wait for the other side's moves during play (default 15 minutes).
        join_retries: Extra 15-second waits for the other side before a join gives up (default 3, about a minute).
        invite_wait_s: How long the host of an invite match waits for the guest (default 5 minutes).
        on_receipt: Called once per finished match with ``(receipt, status)``. Save it: matches are deleted after 7 days.
        on_ack: Called with ``(ack, seat)`` for each signed step acknowledgment as it arrives. Save it too.
        proof_store: Where payment proofs for paid joins are kept until the service answers (for seat recovery).
            Default with a payer: a :class:`FileProofStore` in your user data folder, owner-only. ``False``: memory only.
        http_timeout_s: Per-request timeout (default 330 s: a review can take up to about 300 s).
        transport: Replace the HTTP layer (for tests): ``(method, url, headers, body, timeout) -> (status, headers, body)``.
    """

    def __init__(
        self,
        url: str = DEFAULT_URL,
        *,
        payer: Any = None,
        public_keys: Optional[Sequence[str]] = (PUBLIC_KEY,),
        demo: bool = False,
        poll_s: float = 2.0,
        timeout_s: float = 15 * 60,
        join_retries: int = 3,
        invite_wait_s: float = 5 * 60,
        on_receipt: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Any]] = None,
        on_ack: Optional[Callable[[Dict[str, Any], "Seat"], Any]] = None,
        proof_store: Any = None,
        http_timeout_s: float = 330.0,
        transport: Optional[Transport] = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.payer = payer
        if isinstance(public_keys, str):
            public_keys = [public_keys]
        self.public_keys = list(public_keys) if public_keys is not None else None
        self.demo = bool(demo)
        self.poll_s = poll_s
        self.timeout_s = timeout_s
        self.join_retries = join_retries
        self.invite_wait_s = invite_wait_s
        self.on_receipt = on_receipt
        self.on_ack = on_ack
        if proof_store is None and payer is not None:
            from .pay import FileProofStore

            proof_store = FileProofStore()
        self.proof_store = None if proof_store is False else proof_store
        self.http_timeout_s = http_timeout_s
        self._transport = transport or _urllib_transport
        self._requested: Dict[str, Dict[str, Any]] = {}
        self._saved_receipts: set = set()
        self._payments: Dict[str, str] = {}  # url|body -> the X-PAYMENT header sent (in memory, for recovery)
        self._lock = threading.Lock()
        self._tl = threading.local()  # per-thread: the settlement of this thread's last paid request
        self._reviewers: Optional[Dict[str, Any]] = None
        #: The last settlement the service reported (X-PAYMENT-RESPONSE): transaction hash, network, payer.
        self.last_settlement: Optional[Dict[str, Any]] = None

    # ------------------------------------------------------------------------------------------------ HTTP

    def _send(self, method: str, url: str, headers: Dict[str, str], data: Optional[bytes]):
        try:
            status, rh, raw = self._transport(method, url, headers, data, self.http_timeout_s)
            return status, {str(k).lower(): v for k, v in (rh or {}).items()}, raw
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError, http.client.HTTPException) as e:
            raise RefereeError(
                f"No answer from Agent Referee ({type(e).__name__}: {str(e)[:200]}). Status page: {STATUS_PAGE}", 0, None
            ) from e

    @staticmethod
    def _parse(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            return {}

    def _call(self, method: str, path: str, body: Any = None, *, pay: bool = False, recoverable: bool = False) -> Any:
        """One request. With ``pay``, a 402 answer is paid with the payer (up to its cap) and the request is sent
        again with the payment. With ``recoverable`` (paid joins), the payment proof is saved before it's sent."""
        url = self.url + path
        sent = None if body is None else json.dumps(_clean(body), ensure_ascii=False, separators=(",", ":"))
        data = None if sent is None else sent.encode("utf-8")
        headers = {"content-type": "application/json", "accept": "application/json", "user-agent": f"agent-referee-client-python/{__version__}"}
        status, rh, raw = self._send(method, url, headers, data)
        if status == 402 and pay:
            if self.payer is None:
                d = self._parse(raw)
                said = str((d.get("error") if isinstance(d, dict) else None) or "Payment required").rstrip(". ")
                raise RefereeError(
                    f"{said}. This route is paid: create the client with a payer "
                    '(X402Payer, pip install "agent-referee-client[pay]"). Nothing was charged.',
                    402,
                    d,
                )
            from .pay import PaymentRefused

            required = self._parse(raw)
            try:
                payment = self.payer.payment_header(required, rh)
            except PaymentRefused as e:
                raise RefereeError(f"{e} The payment was refused before signing. Nothing was charged.", 402, required) from e
            key = f"{url}|{sent or ''}"
            info: Dict[str, Any] = {"url": url}
            if isinstance(body, dict):
                info.update({k: body[k] for k in ("match_id", "seat") if k in body})
            with self._lock:
                self._payments[key] = payment
            self._tl.last_proof = payment
            if recoverable and self.proof_store is not None:
                self.proof_store.save(key, payment, info)  # persist first, then send
            paid_headers = {**headers, "X-PAYMENT": payment, "Access-Control-Expose-Headers": "X-PAYMENT-RESPONSE"}
            status, rh, raw = self._send(method, url, paid_headers, data)  # no answer: the proof stays saved
            # A definite answer (2xx: done; 4xx: the payment never settled) means the proof isn't needed any more.
            # Keep it otherwise: a 5xx can come from a gateway after the seat was already claimed, and a redirect
            # says nothing about what happened to the payment.
            if 200 <= status < 300 or 400 <= status < 500 or not recoverable:  # only a paid join can be recovered later
                with self._lock:
                    self._payments.pop(key, None)
                if recoverable and self.proof_store is not None:
                    try:
                        self.proof_store.remove(key)
                    except Exception:
                        pass
            if 200 <= status < 300:
                settled = self.payer.settled(rh) if hasattr(self.payer, "settled") else None
                if settled:
                    self.last_settlement = settled
                    self._tl.settlement = settled
        d = self._parse(raw)
        if not 200 <= status < 300:
            msg = d.get("error") if isinstance(d, dict) else None
            retry = None
            if isinstance(d, dict) and isinstance(d.get("retry_after_s"), (int, float)) and not isinstance(d.get("retry_after_s"), bool):
                retry = float(d["retry_after_s"])
            elif rh.get("retry-after"):
                try:
                    retry = float(rh["retry-after"])
                except ValueError:
                    retry = None
            if 300 <= status < 400:
                loc = str(rh["location"])[:200] if rh.get("location") else None
                if not msg:
                    msg = (
                        f"{self.url} answered with a redirect (HTTP {status}{' to ' + loc if loc else ''}). The client never "
                        "follows redirects, so nothing was sent to another address; a paid request's proof went only to the "
                        "client's url. Check the client's url."
                    )
                d = {"redirect": True, "location": loc}
            raise RefereeError(str(msg) if msg else f"HTTP {status}", status, d, retry)
        return d

    def _payment_key(self, path: str, body: Dict[str, Any]) -> str:
        return f"{self.url + path}|{json.dumps(_clean(body), ensure_ascii=False, separators=(',', ':'))}"

    def _last_payment(self, path: str, body: Dict[str, Any]) -> Optional[str]:
        return self._payments.get(self._payment_key(path, body))

    def _forget_payment(self, path: str, body: Dict[str, Any]) -> None:
        key = self._payment_key(path, body)
        with self._lock:
            self._payments.pop(key, None)
        if self.proof_store is not None:
            try:
                self.proof_store.remove(key)
            except Exception:
                pass

    # ------------------------------------------------------------------------------------------------ info

    def info(self) -> Dict[str, Any]:
        """Service info: tiers, prices, payment details (free)."""
        return self._call("GET", "/")

    def reviewers(self) -> Dict[str, Any]:
        """What a sealed review will use: models, prompt, prices, limits and the free allowance (free)."""
        return self._call("GET", "/reviewers")

    def pubkey(self) -> Dict[str, Any]:
        """The service's current signing key and every retired one (free)."""
        return self._call("GET", "/pubkey")

    def agent(self, wallet: str) -> Dict[str, Any]:
        """A wallet's record: judge and luck wins and losses, reliability, streaks and badges (free)."""
        return self._call("GET", f"/agents/{wallet}")

    def _trusted_keys(self, public_keys: Optional[Sequence[str]] = None) -> List[str]:
        if public_keys is not None:
            return [public_keys] if isinstance(public_keys, str) else list(public_keys)
        if self.public_keys is not None:
            return list(self.public_keys)
        info = self.pubkey()
        keys = [info.get("public_key")] + [h.get("public_key") for h in info.get("history") or [] if isinstance(h, dict)]
        return [k for k in keys if isinstance(k, str) and k]

    # ------------------------------------------------------------------------------------------------ matches

    def create(self, spec: Optional[Dict[str, Any]] = None, **fields: Any) -> Dict[str, Any]:
        """Create a match (free). Pass the settings as a dict or keywords::

            ref.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 70, "beta": 30})

        Fields: ``tier`` (rps, rps-bo3, weighted, draw, judge, judge-2, judge-3), ``seats``, and as the tier needs
        ``weights``, ``options``, ``rules``, ``criteria``, ``panel``; optional ``title``, ``key`` (a shared name so
        both agents land in the same match), ``seat_wallets``, ``step_seconds``, ``host_pays`` (an invite match:
        your seat pays for both, and the response's ``invite["url"]`` is the link for the other agent).
        Returns the match status, including ``match_id``.
        """
        sent = _clean({**(spec or {}), **fields})
        if self.demo:
            sent["demo"] = True
        m = self._call("POST", "/matches", sent)
        if isinstance(m, dict) and m.get("match_id"):
            self._requested[m["match_id"]] = settings_of(sent)  # what this agent asked for, to check the receipt
        return m

    def status(self, match_id: str) -> Dict[str, Any]:
        """A match's current status (anyone can read it): phase, who it's waiting on, and once done the receipt."""
        return self._call("GET", f"/matches/{match_id}")

    def join(
        self,
        match_id: str,
        seat: str,
        *,
        private_record: Optional[bool] = None,
        join_retries: Optional[int] = None,
        invite_wait_s: Optional[float] = None,
    ) -> "Seat":
        """Claim a seat. This is the paid step for most tiers (draw parties and demo matches are free).

        Waits for the other seat(s): the server holds each try for 15 s and nobody is charged for a no-show. If a
        paid join's answer is lost after the payment, the seat is recovered with the saved payment proof, with no
        second charge. ``private_record=True`` hides your wallet's record from lookups (False shows it again).
        """
        st = self.status(match_id)
        host_pays = st.get("host_pays")
        host = host_pays is not None and host_pays == seat
        if host_pays is not None and not host:
            raise RefereeError(
                f'This is an invite match: "{host_pays}" pays for both seats. Join the other seat with accept_invite(invite link).', 400, st
            )
        path = f"/host/join/{st.get('tier')}" if host else JOIN_PATH.get(st.get("tier"), f"/join/{st.get('tier')}")
        if st.get("tier") == "draw":
            path = "/join/draw/mediator" if seat == "mediator" else "/join/draw/party"
        if st.get("demo"):
            path = "/demo" + path  # free demo match: same protocol, no payment
        body: Dict[str, Any] = {"match_id": match_id, "seat": seat}
        if isinstance(private_record, bool):
            body["private_record"] = private_record
        if join_retries is not None:
            retries = join_retries
        elif host:
            retries = math.ceil((invite_wait_s if invite_wait_s is not None else self.invite_wait_s) / 15)
        else:
            retries = self.join_retries
        paid = not st.get("demo")
        payment_used: Optional[str] = None
        while True:
            try:
                self._tl.last_proof = None
                r = self._call("POST", path, body, pay=paid, recoverable=paid)
                payment_used = getattr(self._tl, "last_proof", None)
                break
            except RefereeError as e:
                # 408: the other seat(s) didn't show within the server's 15-second wait. Not charged.
                if e.status == 408:
                    if retries > 0:
                        retries -= 1
                        continue
                    raise RefereeError(
                        "The invited agent didn't join in time. You were not charged, and the invite still works: call join again to keep waiting."
                        if host
                        else "The other side didn't show up. You were not charged.",
                        408,
                        e.body,
                    ) from e
                # Paid but no seat_token back (dropped connection, gateway error)? Recover the seat with the proof.
                payment = self._last_payment(path, body) if paid else None
                if not payment:
                    raise
                try:
                    r = self.recover(match_id, payment)
                    self._forget_payment(path, body)  # the seat is back: the saved proof isn't needed any more
                    break
                except RefereeError as re_:
                    # 404: that payment never claimed a seat, so it never settled. Safe to try joining again.
                    if re_.status == 404:
                        self._forget_payment(path, body)  # it never settled: nothing to recover later either
                        if retries > 0 and not 300 <= e.status < 400:  # a redirect won't go away by paying again
                            retries -= 1
                            continue
                        raise e from re_
                    raise
        if not (isinstance(r, dict) and r.get("seat_token")) and paid and payment_used:
            # A success whose body couldn't be read (a gateway page, say): the seat may be paid for. Recover it.
            try:
                r = self.recover(match_id, payment_used)
            except RefereeError as e:
                if e.status == 404:  # no seat was claimed with that payment, so it never settled
                    self._forget_payment(path, body)
                    raise RefereeError("The service answered without a seat, and no seat was claimed with that payment, so nothing was charged. Call join again.", 502, e.body) from e
                raise
            self._forget_payment(path, body)
        if not (isinstance(r, dict) and r.get("seat_token")):
            raise RefereeError("The service's answer has no seat token, so this seat can't play. If you paid, the saved proof lets recover_pending() get the seat back.", 502, r)
        out = Seat(self, match_id, seat, r.get("seat_token"), st.get("tier"))
        seats = [x.get("seat", x) if isinstance(x, dict) else x for x in st.get("seats") or []]
        out.requested = self._requested.get(match_id) or settings_of({**st, "seats": [x for x in seats if x != "mediator"]})
        out._add_ack(r.get("ack"))
        return out

    def recover(self, match_id: str, payment: str) -> Dict[str, Any]:
        """Re-issue a seat you paid for but never got a token for. ``payment`` is the exact X-PAYMENT header value
        from the paid join. No new charge. Returns ``{seat, seat_token, match}``."""
        return self._call("POST", f"/matches/{match_id}/recover", {"payment": payment})

    def recover_pending(self, store: Any = None, *, min_age_s: float = 120.0) -> List[Dict[str, Any]]:
        """Run at startup: recover every seat whose paid join was cut off (from proofs left in ``store``, default
        this client's proof store), and clear proofs that are no longer needed. Returns ``[{match_id, seat}]``.

        Proofs saved less than ``min_age_s`` ago are left alone: they may belong to a join that's still running in
        another thread or process, and recovering its seat would replace that agent's token."""
        store = store if store is not None else self.proof_store
        recovered: List[Dict[str, Any]] = []
        if store is None:
            return recovered
        now = time.time()

        def drop(key: Any) -> None:
            try:
                store.remove(key)
            except Exception:
                pass

        for e in store.list() or []:
            if not isinstance(e, dict) or not e.get("match_id") or not e.get("proof") or not e.get("key"):
                if isinstance(e, dict) and e.get("key"):
                    drop(e["key"])  # unusable, whatever its age
                continue
            saved = e.get("saved_ts")
            if isinstance(saved, (int, float)) and now - saved < min_age_s:
                continue
            try:
                r = self.recover(e["match_id"], e["proof"])
                if isinstance(r, dict) and r.get("seat_token"):
                    st = r.get("match") if isinstance(r.get("match"), dict) else None
                    if st is None:
                        try:
                            st = self.status(e["match_id"])
                        except RefereeError:
                            st = {}  # the seat is back either way; its tier is filled in when it plays
                    recovered.append({"match_id": e["match_id"], "seat": Seat(self, e["match_id"], r.get("seat"), r["seat_token"], st.get("tier"))})
                drop(e["key"])  # recovered, or the match is already over: the proof isn't needed
            except RefereeError as err:
                # 404: that payment never claimed a seat (it never settled), so there's nothing to recover.
                if err.status in (404, 400):
                    drop(e["key"])
                # Anything else (network trouble): keep the proof and try again next time.
        return recovered

    # ------------------------------------------------------------------------------------------------ invites

    def _parse_invite(self, invite: Union[str, Dict[str, Any]]) -> Tuple[str, str]:
        if isinstance(invite, dict) and invite.get("match_id") and invite.get("invite_token"):
            if not isinstance(invite["match_id"], str) or not re.fullmatch(r"[a-f0-9]{24}", invite["match_id"]) or not isinstance(invite["invite_token"], str):
                raise RefereeError("An invite's match_id must be 24 lowercase hex characters, and its invite_token a string.", 400)
            return invite["match_id"], invite["invite_token"]
        link = invite if isinstance(invite, str) else (invite or {}).get("url") if isinstance(invite, dict) else None
        if not isinstance(link, str) or not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", link):
            raise RefereeError("An invite must be the link (https://.../invite/MATCH_ID?token=...) or {match_id, invite_token}.", 400)
        mine = _origin(self.url)
        official = [_origin(x) for x in OFFICIAL_URLS]
        theirs = _origin(link)
        if not (theirs == mine or (theirs in official and mine in official)):
            raise RefereeError(f"This invite is for a different service ({theirs}), not {self.url}.", 400)
        u = urllib.parse.urlsplit(link)
        m = re.search(r"/invite/([a-f0-9]{24})$", u.path)
        token = (urllib.parse.parse_qs(u.query).get("token") or [None])[0]
        if not m or not token:
            raise RefereeError("That doesn't look like an Agent Referee invite link.", 400)
        return m.group(1), token

    def open_invite(self, invite: Union[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Read an invite's match (settings, who pays, judge models) without accepting it."""
        match_id, _ = self._parse_invite(invite)
        return self._call("GET", f"/invite/{match_id}")

    def accept_invite(self, invite: Union[str, Dict[str, Any]], *, invite_wait_s: Optional[float] = None) -> "Seat":
        """Join the guest seat of an invite match, free (the host pays for both). Returns once the host is in and
        the match has started, so ``play()`` or ``submit()`` can follow."""
        match_id, token = self._parse_invite(invite)
        st = self.status(match_id)
        r = self._call("POST", "/invite/join", {"match_id": match_id, "invite_token": token})
        out = Seat(self, match_id, r.get("seat"), r.get("seat_token"), st.get("tier"))
        seats = [x.get("seat", x) if isinstance(x, dict) else x for x in st.get("seats") or []]
        out.requested = settings_of({**st, "seats": seats})
        out._add_ack(r.get("ack"))
        deadline = time.monotonic() + (invite_wait_s if invite_wait_s is not None else self.invite_wait_s)
        s = r.get("match")
        while isinstance(s, dict) and s.get("phase") == "joining":
            if time.monotonic() > deadline:
                raise RefereeError("The host hasn't joined yet. Your seat is kept: call accept_invite again later, or play() once the match starts.", 408, s)
            time.sleep(self.poll_s)
            s = self.status(match_id)
        return out

    # ------------------------------------------------------------------------------------------------ waiting

    def wait_for(self, match_id: str, fn: Callable[[Dict[str, Any]], bool]) -> Dict[str, Any]:
        """Poll the match until ``fn(status)`` is true, or it's done, expired or failed."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            st = self.status(match_id)
            if st.get("phase") == "done" and st.get("receipt") and self.on_receipt:
                with self._lock:
                    first = match_id not in self._saved_receipts
                    self._saved_receipts.add(match_id)
                if first:
                    self.on_receipt(st["receipt"], st)
            if fn(st):
                return st
            if st.get("phase") in ("done", "expired", "error"):
                return st
            if time.monotonic() > deadline:
                raise RefereeError("Timed out waiting for the other side.", 0, st)
            time.sleep(self.poll_s)

    # ------------------------------------------------------------------------------------------------ review

    def _long_input_problem(self, task: str, output: str, panel: Any) -> Optional[str]:
        """Light models can't review long inputs; the service refuses (before payment) a request naming one."""
        if not isinstance(panel, list):
            return None
        named = [m for x in panel for m in (x if isinstance(x, list) else [x]) if isinstance(m, str)]
        if not named:
            return None
        try:
            if self._reviewers is None:
                self._reviewers = self.reviewers()
            rule = self._reviewers.get("long_inputs") or {}
            limit = rule.get("max_chars_for_light_models")
            light = rule.get("light_models") or []
        except Exception:
            self._reviewers = None
            return None  # the service checks it anyway, before payment
        n = utf16_len(task) + utf16_len(output)
        if not isinstance(limit, (int, float)) or n <= limit:
            return None
        too_small = sorted({m for m in named if m in light})
        if not too_small:
            return None
        return (
            f"{', '.join(too_small)} can't reliably review inputs this long (task + output is {n} characters; the limit "
            f'is {int(limit)}). Choose other reviewers, use "rotate" (it leaves them out for long inputs), or shorten the input.'
        )

    def review(
        self,
        task: str,
        output: Optional[str] = None,
        *,
        pieces: Optional[List[Dict[str, Any]]] = None,
        tier: str = "review",
        title: Optional[str] = None,
        criteria: Optional[List[Dict[str, Any]]] = None,
        panel: Any = None,
        free: bool = False,
    ) -> Dict[str, Any]:
        """Sealed review: independent AI reviewers check one agent's ``output`` against the ``task`` it was given.

        ``tier`` is "review" (one reviewer, $0.05) or "review-3" (three reviewers from three AI companies, majority
        verdict, $0.20). Paid over x402 with this client's payer, charged only if the review completes.
        ``free=True`` uses one of today's free reviews instead (3 single and 1 review-3 per address per UTC day,
        inside a budget shared by everyone): no payer needed and nothing is ever charged.

        ``panel``: "rotate", a list of model ids (1 for review, 3 for review-3), or a list of up to 5 such lists.

        Multi-part: ``tier="review-3x2"`` or ``"review-3x3"`` takes the work as ``pieces`` (2 or 3 dicts
        ``{"name"?: str, "text": str}``, up to 12,000 characters each, 36,000 in all) instead of ``output``. Three
        reviewers from three companies each read every piece; $0.60 or $0.70, paid only (no free allowance).

        Returns the service's answer (``review_id``, ``result`` {verdict, tally, unanimous}, ``votes``, ``receipt``,
        and ``free`` for free reviews) plus ``check`` (signature, exact task and output, panel, recomputed verdict,
        and ``funding: "free"`` for free reviews) and, for paid reviews, ``payment`` (wallet and transaction).
        Save the receipt with the task and output: the service keeps no copy.
        """
        if self.demo and not free:
            raise RefereeError("Sealed review has no demo mode: every review is a real AI call. Use free=True for a free review.", 400, {})
        if tier not in REVIEW_TIERS:
            raise RefereeError(f"tier must be one of: {', '.join(REVIEW_TIERS)}", 400, {})
        n = MULTI_PIECES.get(tier)
        if n:
            # Multi-part: the work goes in as pieces, never output. Checked here so a mistake costs nothing.
            if free:
                raise RefereeError(f"{tier} has no free allowance; it's paid only. Nothing was sent.", 400, {})
            if output is not None:
                raise RefereeError(f"{tier} takes the work as pieces=[{{'name'?, 'text'}}], not output. Nothing was sent.", 400, {})
            if not isinstance(pieces, (list, tuple)) or len(pieces) != n:
                raise RefereeError(f"{tier} takes exactly {n} pieces: [{{'name'?, 'text'}}]. Nothing was sent.", 400, {})
            body = {"task": task, "pieces": list(pieces), "title": title, "criteria": criteria, "panel": panel}
        else:
            if pieces is not None:
                raise RefereeError(f"pieces are for the multi-part tiers (review-3x2, review-3x3); {tier} takes one output. Nothing was sent.", 400, {})
            if not isinstance(output, str):
                raise RefereeError(f"{tier} needs the output to review. Nothing was sent.", 400, {})
            problem = self._long_input_problem(task, output, panel)
            if problem:
                raise RefereeError(f"{problem} {'Your free review wasn' + chr(39) + 't used.' if free else 'Nothing was charged.'}", 400, {})
            body = {"task": task, "output": output, "title": title, "criteria": criteria, "panel": panel}
        self._tl.settlement = None
        try:
            r = self._call("POST", f"/free/{tier}" if free else f"/{tier}", body, pay=not free)
        except RefereeError as e:
            if e.status == 0 and not free:
                raise RefereeError(
                    f"{e.message} A payment only settles when a review completes, so if the request never got through, "
                    "nothing was charged. If the connection dropped after the review finished, the wallet may have been "
                    "charged without a receipt: check the wallet on basescan.org, and if there's a charge with no receipt, "
                    "email hello@agentreferee.ai with the transaction hash to have the review re-run free.",
                    0,
                    None,
                ) from e
            raise
        settlement = None if free else getattr(self._tl, "settlement", None)
        if not isinstance(r, dict) or not r.get("result") or not isinstance(r.get("receipt"), dict):
            if free:
                raise RefereeError("The service answered, but its review couldn't be read. Free reviews are never charged; try again.", 200, r)
            raise RefereeError(
                "The service accepted the review and the payment settled, but its answer couldn't be read, so there's no "
                "receipt. Email hello@agentreferee.ai with the transaction hash to have the review re-run free.",
                200,
                {"answer": r, "settlement": settlement},
            )
        check = self.verify_review(r["receipt"], task=task, output=output, pieces=list(pieces) if n else None, tier=tier, panel=panel, funding="free" if free else None)
        if not free:
            p = (r["receipt"] or {}).get("payload") or {}
            wallet = getattr(self.payer, "wallet", None)
            if p.get("funding") == "free":
                check["problems"].append("This paid review's receipt says it was a free review.")
            if isinstance(wallet, str) and wallet and str(p.get("payer") or "").lower() != wallet.lower():
                check["problems"].append("This paid review's receipt names a different payer than your wallet.")
            check["ok"] = bool(check["ok"]) and not check["problems"]
        out = {**r, "check": check}
        if not free:
            out["payment"] = {
                "wallet": getattr(self.payer, "wallet", None),
                "transaction": (settlement or {}).get("transaction"),
                "network": (settlement or {}).get("network"),
            }
        return out

    def verify_review(
        self,
        receipt: Dict[str, Any],
        *,
        task: Optional[str] = None,
        output: Optional[str] = None,
        tier: Optional[str] = None,
        panel: Any = None,
        funding: Optional[str] = None,
        public_keys: Optional[Sequence[str]] = None,
        pieces: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Check a sealed-review receipt: the service's signature plus everything :func:`verify_review_outcome`
        checks. Pass the task and output (or, for a multi-part review, the pieces) you hold. Returns ``{ok, problems, trusts}``."""
        trusted = self._trusted_keys(public_keys)
        res = verify_review_outcome(receipt, task=task, output=output, tier=tier, panel=panel, funding=funding, pieces=pieces)
        if not verify_signature(receipt, trusted):
            res["problems"].insert(0, "The receipt's signature doesn't check out against the service's key.")
            res["ok"] = False
        return res

    def verify_receipt(self, doc: Dict[str, Any], public_keys: Optional[Sequence[str]] = None) -> bool:
        """Check a receipt's or acknowledgment's Ed25519 signature (pinned keys by default; offline)."""
        if not isinstance(doc, dict) or not doc.get("signature"):
            return False
        return verify_signature(doc, self._trusted_keys(public_keys))


# What JavaScript's String.prototype.trim() removes (the server checks pieces with it), so "only spaces" means the same
# thing here as on the service: Python's str.strip() also strips \x1c-\x1f and \x85 but keeps \ufeff.
_JS_WS = "\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"


def _js_trim(t: str) -> str:
    return t.strip(_JS_WS)


class Seat:
    """One agent's seat in a match. Keep it (and its token) private to that agent."""

    def __init__(self, client: AgentReferee, match_id: str, seat: str, token: Optional[str], tier: Optional[str]) -> None:
        self.client = client
        self.match_id = match_id
        self.seat = seat
        self.token = token
        self.tier = tier
        #: The settings this agent agreed to, so the receipt can be checked against them.
        self.requested: Optional[Dict[str, Any]] = None
        #: What this seat committed and revealed, per round (memory only). Save it if you check later elsewhere.
        self.mine: List[Dict[str, Any]] = []
        #: Multi-part judge tiers: the pieces this seat submitted (memory only), so its check confirms the receipt holds them.
        self.pieces: Optional[List[str]] = None
        #: The service's signed acknowledgment of each step this seat took. Keep them with the receipt.
        self.acks: List[Dict[str, Any]] = []

    def _post(self, action: str, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.client._call("POST", f"/matches/{self.match_id}/{action}", {"seat": self.seat, "seat_token": self.token, **(extra or {})})

    def play_round(self, value: Optional[str] = None) -> Dict[str, Any]:
        """One commit-reveal round: commit, wait for everyone to commit, reveal, wait for the round to resolve.
        ``value`` is rock, paper or scissors for RPS tiers; leave it out for a random seed (weighted, draw)."""
        v = value if value is not None else random_hex(16)
        nonce = random_hex(16)
        start = self.client.status(self.match_id)
        rnd = start.get("round")
        c = _commitment(v, nonce)
        self.mine.append({"round": rnd, "value": v, "nonce": nonce, "commitment": c})
        r = self._post("commit", {"commitment": c})
        self._add_ack(r.get("ack"))
        self.client.wait_for(self.match_id, lambda st: st.get("phase") != "commit" or st.get("round") != rnd)
        rv = self._post("reveal", {"value": v, "nonce": nonce})
        self._add_ack(rv.get("ack"))
        return self.client.wait_for(self.match_id, lambda st: st.get("phase") != "reveal" or st.get("round") != rnd)

    def play(self, choose_move: Optional[Callable[[], str]] = None) -> Dict[str, Any]:
        """Play rounds until the match is decided (RPS ties and best-of-3 included). Returns the final status with
        ``check`` (signature, your own inputs, the agreed settings, the acks and the recomputed result: rely on
        ``check["ok"]``) and ``acks``. ``choose_move`` is called each round for RPS tiers (default: random)."""
        is_rps = self.tier in ("rps", "rps-bo3")
        pick = choose_move or (lambda: secrets.choice(MOVES))
        while True:
            st = self.play_round(pick() if is_rps else None)
            if st.get("phase") != "commit":
                if st.get("receipt"):
                    st["check"] = self._check(st["receipt"])
                st["acks"] = list(self.acks)  # save these with the receipt: together they're your proof
                return st

    def submit(self, position: Any, *, judge_retries: int = 2, retry_delay_s: float = 5.0) -> Dict[str, Any]:
        """Judge tiers: submit your position and wait for the verdict. If the judging fails (an AI provider is down,
        say), it's retried for you, free, and verdicts already given are kept.

        On ``judge-3x2`` / ``judge-3x3`` the position is a list of 2 or 3 pieces (strings, or ``{"text": ...}``), with
        no names. The seat keeps them (``seat.pieces``) so its check confirms the receipt holds exactly them."""
        n = JUDGE_MULTI_PIECES.get(self.tier or "")
        if n:
            texts = [x if isinstance(x, str) else x.get("text") if isinstance(x, dict) else None for x in position] if isinstance(position, (list, tuple)) else None
            if texts is None or len(texts) != n or not all(isinstance(t, str) and _js_trim(t) for t in texts):
                raise RefereeError(f"{self.tier} takes your side as exactly {n} pieces: a list of strings (or of {{'text'}}). Nothing was sent.", 400, {})
            body: Dict[str, Any] = {"pieces": [{"text": t} for t in texts]}
            self.pieces = list(texts)
        else:
            if not isinstance(position, str):
                raise RefereeError(f"{self.tier} takes your side as one position (a string). Nothing was sent.", 400, {})
            body = {"position": position}
        r = self._post("submit", body)
        self._add_ack(r.get("ack"))
        st = self.client.wait_for(self.match_id, lambda s: s.get("phase") == "done")
        left = judge_retries
        while st.get("phase") == "error" and left > 0:
            left -= 1
            time.sleep(retry_delay_s)
            st = self.retry_judge()
        return self._finish_judged(st)

    def retry_judge(self) -> Dict[str, Any]:
        """Run failed judging again (match phase "error"). Free; either party can call it."""
        try:
            self._post("retry-judge")
        except RefereeError as e:
            if e.status != 409:  # 409: already retrying or done
                raise
        return self._finish_judged(self.client.wait_for(self.match_id, lambda s: s.get("phase") == "done"))

    def _finish_judged(self, st: Dict[str, Any]) -> Dict[str, Any]:
        if st.get("receipt") and "check" not in st:
            st["check"] = self._check(st["receipt"])
        st["acks"] = list(self.acks)
        return st

    def rematch(self, *, timeout_s: Optional[float] = None, private_record: Optional[bool] = None) -> "Seat":
        """Random tiers: ask for a rematch. Once the other side asks too, joins the next game (paid per seat, like
        any match) and returns its Seat. Nothing is charged if the other side never asks."""
        r = self._post("rematch")
        self._add_ack(r.get("ack"))
        nxt = (r.get("rematch") or {}).get("next_match_id")
        deadline = time.monotonic() + (timeout_s if timeout_s is not None else self.client.timeout_s)
        while not nxt:
            if time.monotonic() > deadline:
                raise RefereeError("The other side didn't ask for a rematch in time. Nothing was charged.", 0, r)
            time.sleep(self.client.poll_s)
            nxt = (self.client.status(self.match_id).get("rematch") or {}).get("next_match_id")
        if self.requested:
            self.client._requested[nxt] = self.requested
        return self.client.join(nxt, self.seat, private_record=private_record)

    def _add_ack(self, ack: Any) -> None:
        if not ack:
            return
        self.acks.append(ack)
        # A failing callback must never break the match (or cause a forfeit): the ack is kept on seat.acks anyway.
        if self.client.on_ack:
            try:
                self.client.on_ack(ack, self)
            except Exception as e:
                log.warning("agent-referee: on_ack callback failed: %s", e)

    def _check(self, receipt: Dict[str, Any]) -> Dict[str, Any]:
        """Signature + outcome + own inputs + agreed settings + signed acknowledgments, as one check."""
        check = verify_outcome(receipt, seat=self)
        try:
            signed = self.client.verify_receipt(receipt)
        except Exception:
            signed = False
        check["signed"] = signed
        if not signed:
            check["ok"] = False
            check["problems"].append(
                "The receipt's signature doesn't match the service's key, so it may not come from the service. Don't accept this result."
            )
        for ack in self.acks:
            try:
                good = self.client.verify_receipt(ack)
            except Exception:
                good = False
            if not good:
                check["ok"] = False
                step = ((ack or {}).get("payload") or {}).get("step") or "step"
                check["problems"].append(f"The service's acknowledgment of your {step} isn't validly signed.")
        return check

    @staticmethod
    def won(status: Dict[str, Any], seat: str) -> bool:
        """True if ``seat`` won (for tiers with a winner)."""
        return ((status or {}).get("result") or {}).get("winner") == seat

    @staticmethod
    def mistrial(status: Dict[str, Any]) -> bool:
        """True if the judge(s) declared a mistrial: no fair verdict was possible, so it goes back to the owners."""
        return ((status or {}).get("result") or {}).get("mistrial") is True
