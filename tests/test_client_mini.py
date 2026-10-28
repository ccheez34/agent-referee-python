"""The client end to end against tests/js/mini_server.mjs: the real server code for matches, acks, receipts and
reviews, with fake AI answers and emulated payments. Skipped without Node 22.6+ and the server source."""

import os
import threading

import pytest

from agent_referee_client import AgentReferee, MemoryProofStore, RefereeError, Seat, verify_series


def both(fns):
    """Run blocking calls in parallel threads (joins wait for each other) and return their results in order."""
    out, errs = [None] * len(fns), []

    def run(i, f):
        try:
            out[i] = f()
        except Exception as e:  # pragma: no cover - surfaced below
            errs.append(e)

    ts = [threading.Thread(target=run, args=(i, f)) for i, f in enumerate(fns)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(120)
    if errs:
        raise errs[0]
    return out


def client(mini, **kw):
    kw.setdefault("poll_s", 0.1)
    return AgentReferee(mini["url"], public_keys=[mini["key"]], **kw)


def test_demo_weighted_both_seats_check_ok_and_callbacks(mini):
    receipts, acks = [], []
    a = client(mini, demo=True, on_receipt=lambda r, st: receipts.append(r), on_ack=lambda ack, seat: acks.append(ack["payload"]["step"]))
    b = client(mini, demo=True)
    m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 70, "beta": 30}, title="Who drafts")
    ra, rb = both([lambda: a.join(m["match_id"], "alpha").play(), lambda: b.join(m["match_id"], "beta").play()])
    for r in (ra, rb):
        assert r["phase"] == "done" and r["check"]["ok"], r["check"]["problems"]
        assert r["check"]["recomputed"] and r["check"]["signed"]
        assert [x["payload"]["step"] for x in r["acks"]] == ["join", "commit", "reveal"]
    assert ra["receipt"] == rb["receipt"] and ra["receipt"]["payload"]["demo"] is True
    assert len(receipts) == 1 and acks == ["join", "commit", "reveal"]
    assert Seat.won(ra, ra["result"]["winner"])


def test_rps_bo3_ties_and_rounds(mini):
    a, b = client(mini, demo=True), client(mini, demo=True)
    m = a.create(tier="rps-bo3", seats=["x", "y"])
    moves = iter(["rock", "rock", "paper", "paper", "paper"])
    ra, rb = both([lambda: a.join(m["match_id"], "x").play(lambda: next(moves, "paper")), lambda: b.join(m["match_id"], "y").play(lambda: "rock")])
    assert ra["result"]["winner"] == "x" and ra["check"]["ok"] and rb["check"]["ok"], (ra["check"], rb["check"])
    assert len(ra["receipt"]["payload"]["proof"]["rounds"]) >= 3  # a tie replays


def test_draw_three_parties_and_a_mediator(mini):
    med = client(mini, demo=True)
    m = med.create(tier="draw", seats=["planner", "researcher", "writer"], options=["A", "B", "C"])
    parties = [client(mini, demo=True) for _ in range(3)]
    seats = both([lambda c=c, s=s: c.join(m["match_id"], s) for c, s in zip(parties, ["planner", "researcher", "writer"])] + [lambda: med.join(m["match_id"], "mediator")])
    results = both([lambda s=s: s.play() for s in seats])
    assert all(r["check"]["ok"] for r in results), [r["check"]["problems"] for r in results]
    assert results[0]["result"]["picked_option"] in ["A", "B", "C"]


def test_shared_key_lands_both_agents_in_one_match_and_a_mismatch_is_refused(mini):
    a, b = client(mini, demo=True), client(mini, demo=True)
    key = "order-" + os.urandom(6).hex()
    spec = dict(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50}, key=key)
    m1, m2 = a.create(**spec), b.create(**{**spec, "seats": ["beta", "alpha"]})
    assert m1["match_id"] == m2["match_id"]
    with pytest.raises(RefereeError) as e:
        b.create(**{**spec, "weights": {"alpha": 60, "beta": 40}})
    assert e.value.status == 409
    assert {d["field"] for d in e.value.body["differences"]} == {"weights.alpha", "weights.beta"}
    ra, rb = both([lambda: a.join(m1["match_id"], "alpha").play(), lambda: b.join(m1["match_id"], "beta").play()])
    assert ra["check"]["ok"] and rb["check"]["ok"]


def test_none_fields_are_left_out_not_sent_as_null(mini):
    a = client(mini, demo=True)
    m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 1, "beta": 2}, title=None, key=None, options=None)
    assert m["match_id"] and m["title"] is None


def test_invite_match_host_pays_guest_joins_free(mini):
    host, guest = client(mini, demo=True), client(mini, demo=True)
    m = host.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50}, host_pays="alpha")
    link = m["invite"]["url"]
    assert guest.open_invite(link)["match"]["host_pays"] == "alpha"
    with pytest.raises(RefereeError):
        guest.join(m["match_id"], "beta")  # the guest seat is joined with the invite, not join()
    hs, gs = both([lambda: host.join(m["match_id"], "alpha", invite_wait_s=30), lambda: guest.accept_invite(link, invite_wait_s=30)])
    rh, rg = both([hs.play, gs.play])
    assert rh["check"]["ok"] and rg["check"]["ok"], (rh["check"]["problems"], rg["check"]["problems"])
    assert rh["receipt"]["payload"]["host_pays"] == "alpha"


def test_rematch_series(mini):
    a, b = client(mini, demo=True), client(mini, demo=True)
    m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50})
    sa, sb = both([lambda: a.join(m["match_id"], "alpha"), lambda: b.join(m["match_id"], "beta")])
    receipts = []
    for game in range(3):
        ra, rb = both([sa.play, sb.play])
        assert ra["check"]["ok"] and rb["check"]["ok"], (ra["check"]["problems"], rb["check"]["problems"])
        receipts.append(ra["receipt"])
        if game < 2:
            sa, sb = both([lambda: sa.rematch(timeout_s=30), lambda: sb.rematch(timeout_s=30)])
    s = verify_series(receipts)
    assert s["ok"] and s["tally"]["games"] == 3, s["problems"]


def test_free_review_funding_free_allowance_and_busy(mini):
    mini["reset"]()
    ref = client(mini)  # no payer at all
    task = "Translate 'hola' into English. Reply with the single word only."
    r = ref.review(task, "hello", free=True)
    assert r["result"]["verdict"] == "pass" and r["check"]["ok"], r["check"]["problems"]
    assert r["receipt"]["payload"]["funding"] == "free" and r["receipt"]["payload"]["payer"] is None
    assert r["free"]["per_day"] == 3 and "payment" not in r
    ref._call("POST", "/__test", {"busyNextFree": True})
    with pytest.raises(RefereeError) as e:
        ref.review(task, "hello", free=True)
    assert e.value.status == 503 and e.value.retry_after == 7 and "Your free review wasn't used" in e.value.message
    r3 = ref.review(task, "hello", tier="review-3", free=True, panel="rotate")
    assert r3["check"]["ok"] and len(r3["votes"]) == 3, r3["check"]["problems"]
    with pytest.raises(RefereeError) as e:
        ref.review(task, "hello", tier="review-3", free=True)
    assert e.value.status == 429


def test_free_review_with_a_bad_input_keeps_the_service_wording(mini):
    with pytest.raises(RefereeError) as e:
        client(mini).review("short", "x", free=True)
    assert e.value.status == 400 and e.value.message.endswith("Your free review wasn't used.")


def test_long_input_naming_a_light_model_is_refused_before_sending(mini):
    mini["reset"]()
    calls = []
    ref = client(mini)
    real = ref._transport

    def counting(method, url, headers, body, timeout):
        calls.append((method, url))
        return real(method, url, headers, body, timeout)

    ref._transport = counting
    with pytest.raises(RefereeError) as e:
        ref.review("Summarise this document in five bullet points.", "x" * 6500, panel=["claude-haiku-5-5"], free=True)
    assert e.value.status == 400 and "claude-haiku-5-5" in e.value.message
    assert [u for m, u in calls if m == "POST"] == []  # only GET /reviewers was sent
    # rotate leaves light models out by itself, so it goes through:
    assert ref.review("Summarise this document in five bullet points.", "x" * 6500, panel="rotate", free=True)["check"]["ok"]


def test_paid_routes_without_a_payer_fail_with_402_and_nothing_charged(mini):
    ref = client(mini)
    with pytest.raises(RefereeError) as e:
        ref.review("Translate 'hola' into English, one word.", "hello", tier="review")
    assert e.value.status == 402 and "Nothing was charged" in e.value.message


# ---------- paid (emulated payments through x402's own v1 code, with a stand-in signer) ----------


def test_paid_review_and_review3(mini, fake_payer):
    payer = fake_payer(200_000)
    ref = client(mini, payer=payer, proof_store=False)
    task = "Write a SQL query returning the 10 newest orders."
    out = "SELECT * FROM orders ORDER BY created_at DESC LIMIT 10;"
    r = ref.review(task, out)
    assert r["check"]["ok"], r["check"]["problems"]
    assert r["receipt"]["payload"]["payer"] == payer.wallet.lower() and "funding" not in r["receipt"]["payload"]
    assert r["payment"]["transaction"] == "0x" + "cd" * 32 and payer.scheme.signed == [50_000]
    r3 = ref.review(task, out, tier="review-3", panel=["claude-opus-5-5", "openai/gpt-6-luna", "google/gemini-3.7-flash"])
    assert r3["check"]["ok"] and payer.scheme.signed == [50_000, 200_000], r3["check"]["problems"]


def test_price_above_the_cap_is_refused_before_signing(mini, fake_payer):
    payer = fake_payer(50_000)
    ref = client(mini, payer=payer, proof_store=False)
    with pytest.raises(RefereeError) as e:
        ref.review("Write a SQL query returning the 10 newest orders.", "SELECT 1;", tier="review-3")
    assert e.value.status == 402 and "refused before signing" in e.value.message and "Nothing was charged" in e.value.message
    assert payer.scheme.signed == []


def test_paid_weighted_match_proof_saved_before_sending_then_removed(mini, fake_payer, tmp_path):
    from agent_referee_client import FileProofStore

    store = FileProofStore(str(tmp_path / "proofs"))
    seen = []
    a = client(mini, payer=fake_payer(15_000, "0x1111111111111111111111111111111111111111"), proof_store=store)
    b = client(mini, payer=fake_payer(15_000, "0x2222222222222222222222222222222222222222"), proof_store=MemoryProofStore())
    real = a._transport

    def watching(method, url, headers, body, timeout):
        if "X-PAYMENT" in headers:
            seen.append(len(store.list()))  # the proof is on disk before the paid request goes out
        return real(method, url, headers, body, timeout)

    a._transport = watching
    m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50})
    ra, rb = both([lambda: a.join(m["match_id"], "alpha").play(), lambda: b.join(m["match_id"], "beta").play()])
    assert ra["check"]["ok"] and rb["check"]["ok"], (ra["check"]["problems"], rb["check"]["problems"])
    assert seen and all(n == 1 for n in seen) and store.list() == []
    assert ra["receipt"]["payload"]["paid_by"]["alpha"] == "0x1111111111111111111111111111111111111111"
    if os.name == "posix":
        assert (os.stat(tmp_path / "proofs").st_mode & 0o777) == 0o700


def test_dropped_reply_after_a_paid_join_recovers_the_seat_without_paying_again(mini, fake_payer):
    pa, pb = fake_payer(15_000, "0x1111111111111111111111111111111111111111"), fake_payer(15_000, "0x2222222222222222222222222222222222222222")
    store = MemoryProofStore()
    a = client(mini, payer=pa, proof_store=store)
    b = client(mini, payer=pb, proof_store=MemoryProofStore())
    m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50})
    sb_holder = {}
    t = threading.Thread(target=lambda: sb_holder.update(seat=b.join(m["match_id"], "beta")))
    t.start()
    a._call("POST", "/__test", {"dropSeat": "alpha"})
    sa = a.join(m["match_id"], "alpha")
    t.join(60)
    ra, rb = both([sa.play, sb_holder["seat"].play])
    assert ra["check"]["ok"] and rb["check"]["ok"], (ra["check"]["problems"], rb["check"]["problems"])
    assert len(pa.scheme.signed) == 1  # paid once
    assert [x["payload"]["step"] for x in sa.acks] == ["commit", "reveal"]  # recovery issues no join ack (as documented)
    assert store.list() == []  # the proof is gone once the seat was recovered


def test_recover_pending_at_startup(mini, fake_payer):
    pa, pb = fake_payer(15_000, "0x1111111111111111111111111111111111111111"), fake_payer(15_000, "0x2222222222222222222222222222222222222222")
    store = MemoryProofStore()
    a = client(mini, payer=pa, proof_store=store)
    b = client(mini, payer=pb, proof_store=MemoryProofStore())
    m = a.create(tier="weighted", seats=["alpha", "beta"], weights={"alpha": 50, "beta": 50})
    holder = {}
    t = threading.Thread(target=lambda: holder.update(seat=b.join(m["match_id"], "beta")))
    t.start()
    a._call("POST", "/__test", {"dropSeat": "alpha"})
    a.join_retries = 0
    a.recover = lambda *x, **k: (_ for _ in ()).throw(RefereeError("simulated crash before recovery", 0))
    with pytest.raises(RefereeError):
        a.join(m["match_id"], "alpha")
    t.join(60)
    assert len(store.list()) == 1  # the proof survived the "crash"
    fresh = client(mini, payer=pa, proof_store=store)  # a restart
    assert fresh.recover_pending() == []  # too fresh: it could be a join still running elsewhere
    rec = fresh.recover_pending(min_age_s=0)
    assert len(rec) == 1 and rec[0]["match_id"] == m["match_id"] and store.list() == []
    ra, rb = both([rec[0]["seat"].play, holder["seat"].play])
    assert ra["phase"] == "done" and rb["check"]["ok"]


def test_judge3_rotate_and_picks_with_fake_judges(mini, fake_payer):
    for panel in ["rotate", {"picks": {"buyer": "claude-opus-5-5", "seller": "openai/gpt-6-luna"}}]:
        a = client(mini, payer=fake_payer(350_000, "0x1111111111111111111111111111111111111111"), proof_store=False)
        b = client(mini, payer=fake_payer(350_000, "0x2222222222222222222222222222222222222222"), proof_store=False)
        m = a.create(tier="judge-3", seats=["buyer", "seller"], rules="The cheaper plan wins. Equal prices: the faster one.", panel=panel)
        sa, sb = both([lambda: a.join(m["match_id"], "buyer"), lambda: b.join(m["match_id"], "seller")])
        ra, rb = both([lambda: sa.submit("Plan A: $10, 3 days."), lambda: sb.submit("Plan B: $12, 1 day.")])
        assert ra["check"]["ok"] and rb["check"]["ok"], (ra["check"]["problems"], rb["check"]["problems"])
        assert [x["payload"]["step"] for x in ra["acks"]] == ["join", "submit"]


def test_paid_review_receipt_must_name_this_payer(mini, fake_payer):
    ref = client(mini, payer=fake_payer(50_000, "0x3333333333333333333333333333333333333333"), proof_store=False)
    r = ref.review("Write a SQL query returning the 10 newest orders.", "SELECT 1;")
    assert r["check"]["ok"]
    ref.payer.wallet = "0x4444444444444444444444444444444444444444"  # as if the receipt were someone else's
    r = ref.review("Write a SQL query returning the 10 newest orders.", "SELECT 1;")
    assert not r["check"]["ok"] and "different payer" in " ".join(r["check"]["problems"])
