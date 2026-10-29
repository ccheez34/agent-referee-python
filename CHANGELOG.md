# Changelog

## 0.3.0 (unreleased)

- **Multi-part judge matches (`judge-3x2`, `judge-3x3`).** `seat.submit([text1, text2])` sends a side's case as 2 or
  3 pieces (strings, or `{"text": ...}`). A wrong number of pieces, an empty piece, or a single position on these
  tiers (or pieces on `judge`, `judge-2`, `judge-3`) is refused before anything is sent. The seat keeps its pieces
  (`seat.pieces`) so its check confirms the receipt holds exactly them.
- **`judge-multi-v1` receipts are checked**, not refused: `verify_outcome(receipt, pieces={seat: texts}, acks=...)`
  confirms the judges' instruction version, that there's no `submission_sha256`, each side's piece records and
  `pieces_sha256`, both sides' pieces in a judged match (only the submitting side's in a forfeit), three judges from
  three AI companies, the majority winner and tally, and, given pieces, their count, order, SHA-256 and length. A
  signed submit acknowledgment whose pieces differ from the receipt, or a side listed as forfeiting after its
  submission was accepted, is reported as provable evidence. Tested against the server's 20 signed test vectors
  (`tests/test_multi_judge.py`), with the same messages as the JS client 0.10.0.
- Still refused: unknown receipt formats, `judge-multi-v1` on `judge-3` (or any other tier), a multi-part judge tier
  without `receipt_format`, and a review format on a match receipt.

## 0.2.0 (2026-10-28)

- **Multi-part reviews (`review-3x2`, `review-3x3`).** `ref.review(task, pieces=[{"name"?, "text"}, ...],
  tier="review-3x2")` sends the work as 2 or 3 pieces, and three reviewers from three companies each read every
  piece. Mistakes (an `output`, the wrong number of pieces, or `free=True`, since these tiers are paid only) are
  refused before anything is sent or paid for. `output` is now optional in `review()` (required for the single-piece
  tiers, as before).
- **`review-multi-v1` receipts are checked**, not refused: `verify_review_outcome` and `verify_review` take `pieces`
  and confirm each piece's SHA-256, length (UTF-16 code units) and name in order, the `pieces_sha256` of the list,
  that there's no `output_sha256`, three votes from three AI companies each with `issues_dropped`, and the majority
  verdict and tally. Tested against the server's 17 signed test vectors (`tests/test_multi.py`), with the same
  messages as the JS client 0.9.0.
- Still refused: `judge-multi-v1` and any other format this version doesn't know, a `review-multi-v1` receipt on a
  single-piece tier or a match receipt, and a multi-part tier without `receipt_format`.
- `KNOWN_RECEIPT_FORMATS` is now a dict by kind of receipt (`{"review": (...), "match": (...)}`).

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
