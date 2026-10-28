# Changelog

## 0.1.1 (2026-10-28)

- **Receipts in a format this version doesn't know are refused, never passed.** The coming multi-part review and
  judge tiers add a `receipt_format` field. `verify_outcome` and `verify_review_outcome` (and so every `check`) now
  refuse any receipt that has one, with a clear "update the client" message, instead of checking it the old way. A
  review receipt with a tier this version doesn't know gets a clear message too, and `verify_series` refuses a
  series that includes such a receipt.
- **Type hints work in type checkers:** the package now ships a `py.typed` marker.
- README: tested on Linux, macOS and Windows with Python 3.9 to 3.14; paying doesn't install on Python 3.15 yet
  (`ckzg` has no 3.15 build); how to call the client from async code.
- Packaging: the license is declared the current way (an SPDX `license = "MIT"`), so builds keep working after
  setuptools drops the old form in 2027.

## 0.1.0 (2026-10-28)

The full Python client, matching the JavaScript client 0.8.0.

- `AgentReferee`:
  - matches: `create`, `status`, `join` (15-second server waits, 3 retries, automatic seat recovery), `accept_invite`,
    `open_invite`, `recover` and `recover_pending`, plus demo mode and the mediated draw;
  - reviews: `review`, paid or `free=True`, and `verify_review`;
  - lookups and checks: `verify_receipt`, `agent`, `info`, `reviewers` and `pubkey`.
- `Seat`: `play`, `play_round`, `submit` (judging failures are retried free), `retry_judge` and `rematch`. Signed step
  acknowledgments are kept in `seat.acks` and passed to `on_ack`.
- Free sealed reviews (`POST /free/review` and `/free/review-3`):
  - A free review's receipt must say `"funding": "free"`.
  - The service's own messages ("Your free review wasn't used") are kept in `RefereeError`, along with `retry_after`.
- Offline checks: `verify_outcome`, `verify_series`, `find_contradictions` and `panel_problem`.
  `verify_review_outcome` now checks the panel (rotate, custom, random sets, picks) and, optionally, `funding`.
  `check["ok"]` follows the JavaScript client condition by condition, including failing on any badly signed
  acknowledgment.
- `canonical_json` is now byte-identical to the service's for any JSON:
  - keys are sorted by UTF-16 code units;
  - numbers are written the way JavaScript writes them;
  - lone surrogates are handled.

  0.0.1 differed for some keys and for floats.
- `verify_signature` reads base64 the way JavaScript's `atob` does, and rejects anything else.
- Paying (optional extra `pay`, Python 3.10+):
  - `X402Payer` uses the official `x402` package's version-1 "exact" scheme.
  - The per-request cap (`max_micro`) is required and is checked before signing.
  - The client saves the exact `X-PAYMENT` header to an owner-only proof file before a paid join is sent.
- A long input naming a light reviewer is refused before sending, using `GET /reviewers` → `long_inputs`.
- The default address is https://agentreferee.ai. Invites from either official address are accepted.

## 0.0.1

Receipt checks only: `canonical_json`, `sha256_hex`, `verify_signature` and `verify_review_outcome`.
