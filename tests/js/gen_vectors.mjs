// Produces tests/vectors.json: expected values computed by the SERVER's own code and the JavaScript client 0.8.0,
// which the Python tests must reproduce exactly. Run again whenever either changes:
//
//   node --experimental-strip-types tests/js/gen_vectors.mjs ..\agent-referee-server\src ..\agent-referee-client\src\index.mjs tests\vectors.json
//
// It starts tests/js/mini_server.mjs (the real match and review code with fake AI) and plays matches through the JS
// client, then tampers with copies of the receipts. Every receipt is signed with a throwaway key from that run.
import { spawn } from "node:child_process";
import { writeFile } from "node:fs/promises";
import { join, dirname, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const [srcDir, clientPath, outPath] = process.argv.slice(2);
const here = dirname(fileURLToPath(import.meta.url));
const mod = (f) => import(pathToFileURL(join(resolve(srcDir), f)).href);
const logic = await mod("logic.ts");
const review = await mod("review.ts");
const { signReceipt } = await mod("receipt.ts");
const js = await import(pathToFileURL(resolve(clientPath)).href);

const out = { generated_by: "tests/js/gen_vectors.mjs", canonical: [], strings: [], randomness: [], reviews: [], matches: [], series: [] };

// ---------- canonical JSON and hashing (server logic.ts) ----------
const jsonInputs = [
  '{"b":1,"a":[true,null,"x"],"c":{"z":{},"y":[]}}',
  '{"\\uff01":1,"\\ud83d\\ude00":2,"a":3,"B":4,"_":5,"é":6,"aa":7,"a\\u0000":8}',
  '{"s":"Traduce «hola» — \\"q\\" \\\\ back\\nnew\\tline\\r\\b\\f\\u0001\\u001f\\u007f\\u2028\\u2029 ✓ 👋"}',
  '{"lone":"x\\ud800y","lone2":"\\udfff","pair":"\\ud83d\\ude00"}',
  '[1e21,1e-7,0.1,5e-324,123456789012345680000,-0,1.5,1e-6,123.456,1e300,-2.5e-8,100,0.000001234,9007199254740991,-1,0]',
  '{"n":1.0,"m":2.50,"e":1E3,"big":12345678901234567890}',
  '{"nested":[{"k2":"v","k1":[{"y":1,"x":2}]}],"empty":"","t":true,"f":false,"z":null}',
];
for (const j of jsonInputs) {
  const v = JSON.parse(j);
  const c = logic.canonicalJson(v);
  out.canonical.push({ json: j, canonical: c, sha256: await logic.sha256Hex(c) });
}
for (const s of ["", "hello 👋", "x\ud800y", "😀", "Ünïcode «» — ✓", "a".repeat(5000), "line\r\nbreak"]) {
  out.strings.push({ text: s, sha256: await logic.sha256Hex(s), length: s.length });
}

// ---------- commitments and joint randomness ----------
for (let i = 0; i < 12; i++) {
  const seeds = { alpha: logic.randomToken(16), beta: logic.randomToken(16), ...(i % 3 === 0 ? { gamma: logic.randomToken(16) } : {}) };
  const matchId = logic.randomToken(12);
  const nonce = logic.randomToken(16);
  const { hex, value } = await logic.combinedRandom(matchId, 1 + (i % 2), seeds);
  const weights = { alpha: 1 + i * 37, beta: 1000 - i * 41, ...(seeds.gamma ? { gamma: 3 } : {}) };
  const options = ["A", "B", "C", "D", "E"].slice(0, 2 + (i % 4));
  out.randomness.push({
    match_id: matchId,
    round: 1 + (i % 2),
    seeds,
    combined_hex: hex,
    combined_value: value.toString(),
    weights,
    weighted_pick: logic.weightedPick(weights, value),
    options,
    draw_pick: logic.drawPick(options, value),
    commit_value: seeds.alpha,
    nonce,
    commitment: await logic.commitmentOf(seeds.alpha, nonce),
  });
}

// ---------- the mini server and the JS client ----------
const child = spawn(process.execPath, ["--experimental-strip-types", "--no-warnings", join(here, "mini_server.mjs"), resolve(srcDir)], { stdio: ["ignore", "pipe", "inherit"] });
const first = await new Promise((res) => child.stdout.once("data", (d) => res(JSON.parse(String(d).split("\n")[0]))));
const url = `http://127.0.0.1:${first.port}`;
out.public_key = first.public_key;
const keys = [first.public_key];
const SIGNING_KEY_NOTE = "receipts in this file are signed by the mini server's throwaway key (public_key above)";
out.note = SIGNING_KEY_NOTE;

const clone = (x) => structuredClone(x);
const seatState = (s) => ({ matchId: s.matchId, seat: s.seat, mine: s.mine, acks: s.acks, requested: s.requested });
// Re-sign a tampered payload with the same key, so only the content check (not the signature) can catch it.

async function matchCase(name, receipt, seat, extra = {}) {
  const withSeat = seat ? await js.verifyOutcome(receipt, { seat }) : null;
  const without = await js.verifyOutcome(receipt);
  out.matches.push({
    name,
    receipt,
    seat: seat ? seatState(seat) : null,
    signed: await js.verifySignature(receipt, keys),
    with_seat: withSeat,
    without_seat: without,
    contradictions: seat ? js.findContradictions(receipt, seat.acks).map((e) => e.reason) : [],
    ...extra,
  });
}

async function playBoth(spec, seatsToPlay, moves) {
  const a = new js.AgentReferee({ url, demo: spec.demo !== false, publicKeys: keys, pollMs: 100 });
  const b = new js.AgentReferee({ url, demo: spec.demo !== false, publicKeys: keys, pollMs: 100 });
  const { demo, ...rest } = spec;
  const m = await a.create(rest);
  const clients = seatsToPlay.map((_, i) => (i === 0 ? a : i === 1 ? b : new js.AgentReferee({ url, demo: true, publicKeys: keys, pollMs: 100 })));
  const seats = await Promise.all(seatsToPlay.map((s, i) => clients[i].join(m.match_id, s)));
  const results = await Promise.all(seats.map((s, i) => (moves ? s.play(() => moves[i]) : s.play())));
  return { m, seats, results, clients };
}

// Weighted, both seats.
{
  const { seats, results } = await playBoth({ tier: "weighted", seats: ["alpha", "beta"], weights: { alpha: 70, beta: 30 } }, ["alpha", "beta"]);
  const r = results[0].receipt;
  await matchCase("weighted ok", r, seats[0]);
  const t1 = clone(r); t1.payload.outcome.winner = t1.payload.outcome.winner === "alpha" ? "beta" : "alpha";
  await matchCase("weighted winner swapped", t1, seats[0]);
  const t2 = clone(r); t2.payload.proof.reveals.alpha.value = "f".repeat(32);
  await matchCase("weighted reveal changed", t2, seats[0]);
  const t3 = clone(r); t3.payload.proof.commits.alpha = "0".repeat(64);
  await matchCase("weighted my commitment replaced", t3, seats[0]);
  const t4 = clone(r); t4.payload.outcome.weights = { alpha: 50, beta: 50 };
  await matchCase("weighted odds changed", t4, seats[0]);
  const t5 = clone(r); t5.payload.demo = false;
  await matchCase("weighted demo flag dropped", t5, seats[1]);
  const t6 = clone(r); t6.payload.proof.combined_sha256 = "1".repeat(64);
  await matchCase("weighted combined hash wrong", t6, seats[1]);
  const t7 = clone(r); t7.payload.match_id = "0".repeat(24);
  await matchCase("weighted other match", t7, seats[1]);
  const t8 = clone(r); t8.signature = r.signature.replace(/^./, r.signature[0] === "A" ? "B" : "A");
  await matchCase("weighted bad signature", t8, seats[0]);
  const t9 = clone(r); t9.payload.outcome.forfeited = ["beta"]; t9.payload.outcome.by_forfeit = true;
  await matchCase("weighted false forfeit", t9, seats[1]);
}
// RPS best of 3, fixed moves so there are ties and several rounds.
{
  const { seats, results } = await playBoth({ tier: "rps-bo3", seats: ["x", "y"] }, ["x", "y"], ["rock", "scissors"]);
  const r = results[0].receipt;
  await matchCase("rps-bo3 ok", r, seats[0]);
  const t1 = clone(r); t1.payload.proof.rounds[0].winner = "y";
  await matchCase("rps-bo3 round winner changed", t1, seats[1]);
  const t2 = clone(r); t2.payload.proof.rounds[0].reveals.x.nonce = "0".repeat(32);
  await matchCase("rps-bo3 reveal nonce changed", t2, seats[0]);
  const t3 = clone(r); t3.payload.outcome.winner = null;
  await matchCase("rps-bo3 no winner", t3, null);
  const t4 = clone(r); t4.payload.parties = ["x", "z"];
  await matchCase("rps-bo3 seats changed", t4, seats[0]);
}
{
  const { seats, results } = await playBoth({ tier: "rps", seats: ["p", "q"] }, ["p", "q"]);
  await matchCase("rps random ok", results[0].receipt, seats[1]);
}
// Draw: three parties and a mediator.
{
  const med = new js.AgentReferee({ url, demo: true, publicKeys: keys, pollMs: 100 });
  const m = await med.create({ tier: "draw", seats: ["planner", "researcher", "writer"], options: ["Option A", "Option B", "Option C", "Option D"] });
  const cl = ["planner", "researcher", "writer"].map(() => new js.AgentReferee({ url, demo: true, publicKeys: keys, pollMs: 100 }));
  const parties = await Promise.all(["planner", "researcher", "writer"].map((s, i) => cl[i].join(m.match_id, s)));
  const ms = await med.join(m.match_id, "mediator");
  const all = await Promise.all([...parties, ms].map((s) => s.play()));
  const r = all[0].receipt;
  await matchCase("draw ok", r, parties[0]);
  await matchCase("draw ok (mediator)", r, ms);
  const t1 = clone(r); t1.payload.outcome.picked_index = (r.payload.outcome.picked_index + 1) % 4; t1.payload.outcome.picked_option = r.payload.outcome.options[t1.payload.outcome.picked_index];
  await matchCase("draw pick changed", t1, parties[1]);
  const t2 = clone(r); t2.payload.outcome.options = ["Option A", "Option B", "Option C", "Option E"];
  await matchCase("draw options changed", t2, parties[2]);
}
// Judge panels with fake AI (paid tiers, payments emulated through a stand-in payer header).
async function payingFetch(input, init = {}) {
  let res = await fetch(input, init);
  if (res.status !== 402) return res;
  const body = JSON.parse(init.body);
  const header = Buffer.from(JSON.stringify({ x402Version: 1, scheme: "exact", network: "base", payload: { authorization: { from: body.seat === "buyer" ? "0x1111111111111111111111111111111111111111" : "0x2222222222222222222222222222222222222222", to: "0x0", value: "1", validAfter: "0", validBefore: "9999999999", nonce: "0x" + logic.randomToken(32) }, signature: "0x" + "11".repeat(65) } })).toString("base64");
  const headers = new Headers(init.headers || {});
  headers.set("X-PAYMENT", header);
  return fetch(input, { ...init, headers });
}
for (const [tier, panel] of [["judge", undefined], ["judge-2", undefined], ["judge-3", "rotate"], ["judge-3", { picks: { buyer: "claude-opus-5-5", seller: "openai/gpt-6-luna" } }], ["judge-3", [["claude-sonnet-5-5", "openai/gpt-6-luna", "google/gemini-4-argon"], ["claude-opus-5-5", "openai/gpt-6-luna", "google/gemini-3.7-flash"]]]]) {
  const a = new js.AgentReferee({ url, fetch: payingFetch, publicKeys: keys, pollMs: 100 });
  const b = new js.AgentReferee({ url, fetch: payingFetch, publicKeys: keys, pollMs: 100 });
  const spec = { tier, seats: ["buyer", "seller"], rules: "The cheaper plan wins. Equal prices: the faster one.", ...(panel ? { panel } : {}) };
  const m = await a.create(spec);
  const [sa, sb] = await Promise.all([a.join(m.match_id, "buyer"), b.join(m.match_id, "seller")]);
  const [ra] = await Promise.all([sa.submit("Plan A costs $10 and takes 3 days."), sb.submit("Plan B costs $12 and takes 1 day.")]);
  const r = ra.receipt;
  const label = `${tier}${panel ? " " + (typeof panel === "string" ? panel : Array.isArray(panel) ? "sets" : "picks") : ""}`;
  await matchCase(`${label} ok`, r, sa);
  if (tier !== "judge") {
    const t1 = clone(r); t1.payload.outcome.votes[0].model = "claude-haiku-5-5";
    await matchCase(`${label} vote from another model`, t1, sa);
    const t2 = clone(r); t2.payload.outcome.winner = "seller";
    await matchCase(`${label} decision changed`, t2, sb);
    const t3 = clone(r); t3.payload.outcome.tally = { buyer: 0, seller: 3, mistrial: 0 };
    await matchCase(`${label} tally changed`, t3, null);
  }
  const t4 = clone(r); t4.payload.proof.rules_sha256 = "2".repeat(64);
  await matchCase(`${label} rules changed`, t4, sa);
  const t5 = clone(r); t5.payload.proof.submission_sha256 = { ...(t5.payload.proof.submission_sha256 ?? {}), buyer: "3".repeat(64) };
  await matchCase(`${label} submission changed`, t5, sa);
  if (panel) {
    const t6 = clone(r); t6.payload.proof.judge.panel_selection = { mode: "custom" };
    await matchCase(`${label} panel selection changed`, t6, sb);
  }
}
// Rematch series (weighted, demo).
{
  const a = new js.AgentReferee({ url, demo: true, publicKeys: keys, pollMs: 100 });
  const b = new js.AgentReferee({ url, demo: true, publicKeys: keys, pollMs: 100 });
  const m = await a.create({ tier: "weighted", seats: ["alpha", "beta"], weights: { alpha: 50, beta: 50 } });
  let [sa, sb] = await Promise.all([a.join(m.match_id, "alpha"), b.join(m.match_id, "beta")]);
  const receipts = [];
  for (let g = 0; g < 3; g++) {
    const [r] = await Promise.all([sa.play(), sb.play()]);
    receipts.push(r.receipt);
    if (g < 2) [sa, sb] = await Promise.all([sa.rematch(), sb.rematch()]);
  }
  const add = async (name, rs) => out.series.push({ name, receipts: rs, expected: await js.verifySeries(rs) });
  await add("series ok", receipts);
  await add("series out of order", [receipts[2], receipts[0], receipts[1]]);
  await add("series from the middle", [receipts[1], receipts[2]]);
  await add("series missing game", [receipts[0], receipts[2]]);
  const t = clone(receipts); t[0].payload.outcome.winner = t[0].payload.outcome.winner === "alpha" ? "beta" : "alpha";
  await add("series game 1 changed", t);
  const u = clone(receipts); u[2].payload.series.tally.wins.alpha += 1;
  await add("series tally wrong", u);
  await matchCase("series game 3 ok", receipts[2], sa);
}

// ---------- sealed reviews (server review.ts payloads, JS client checks) ----------
const signed = (payload) => signReceipt(payload, out._sk, out._pk);
{
  // A separate throwaway key for review fixtures built directly from reviewPayload.
  const kp = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
  out._sk = Buffer.from(await crypto.subtle.exportKey("pkcs8", kp.privateKey)).toString("base64");
  out._pk = Buffer.from(await crypto.subtle.exportKey("raw", kp.publicKey)).toString("base64");
}
const env = { MODEL_POOL: undefined };
const task = 'Traduce «hola» al inglés — solo la palabra. "Quote" \\ back\nnew line ✓ 😀';
const output = "hello 👋";
const criteria = [{ name: "accuracy", weight: 3 }, { name: "brevity", weight: 1 }];
const mkVote = (i, model, verdict, sc) => ({ reviewer: i + 1, model, verdict, confidence: "high", issues: verdict === "pass" ? [] : ["Emoji «x» 😀"], reasoning: "Fine.\tTab", ...(sc ? { scores: sc, total: 3 * sc.accuracy + sc.brevity } : {}) });
async function reviewCase(name, receipt, opts, extra = {}) {
  out.reviews.push({ name, receipt, opts, signed: await js.verifySignature(receipt, [out._pk]), expected: await js.verifyReviewOutcome(receipt, opts), ...extra });
}
for (const [label, tier, panel, funding] of [
  ["review-3 default", "review-3", undefined, undefined],
  ["review-3 rotate", "review-3", "rotate", undefined],
  ["review-3 custom", "review-3", ["claude-opus-5-5", "openai/gpt-6-luna", "google/gemini-3.7-flash"], undefined],
  ["review-3 sets", "review-3", [["claude-opus-5-5", "openai/gpt-6-luna", "google/gemini-3.7-flash"], ["claude-sonnet-5-5", "openai/gpt-6-luna", "google/gemini-4-argon"]], undefined],
  ["review single", "review", undefined, undefined],
  ["review free", "review", undefined, "free"],
  ["review-3 free rotate", "review-3", "rotate", "free"],
]) {
  const commitment = await review.reviewCommitment(tier, env, panel, undefined, task.length + output.length);
  const verdicts = tier === "review" ? ["needs_changes"] : ["pass", "pass", "needs_changes"];
  const votes = commitment.models.map((m, i) => mkVote(i, m, verdicts[i], { accuracy: 9 - i, brevity: 8 }));
  const payload = await review.reviewPayload({ tier, input: { task, output, title: "Ünïcode", criteria }, commitment, votes, payer: funding ? undefined : "0xabc", network: "base", id: logic.randomToken(12), ...(funding ? { funding } : {}) });
  const receipt = await signed(payload);
  const opts = { task, output, tier, ...(panel ? { panel } : {}) };
  await reviewCase(`${label} ok`, receipt, opts, { funding: funding ?? null });
  if (label === "review-3 default") {
    await reviewCase(`${label} other output`, receipt, { ...opts, output: "hello" });
    await reviewCase(`${label} other task`, receipt, { ...opts, task: "something else entirely" });
    await reviewCase(`${label} other tier`, receipt, { ...opts, tier: "review" });
    await reviewCase(`${label} asked rotate`, receipt, { ...opts, panel: "rotate" });
    const t1 = clone(receipt); t1.payload.result.verdict = "split";
    await reviewCase(`${label} verdict changed`, t1, opts);
    const t2 = clone(receipt); t2.payload.votes[0].total = 1;
    await reviewCase(`${label} total changed`, t2, opts);
    const t3 = clone(receipt); t3.payload.votes[1].model = "claude-haiku-5-5";
    await reviewCase(`${label} vote model changed`, t3, opts);
    const t4 = clone(receipt); t4.payload.result.tally.fail = 1;
    await reviewCase(`${label} tally changed`, t4, opts);
    const t5 = clone(receipt); t5.payload.reviewers.decision_rule = "best-of";
    await reviewCase(`${label} unknown rule`, t5, opts);
    const t6 = clone(receipt); t6.payload.votes.pop();
    await reviewCase(`${label} vote missing`, t6, opts);
    const t7 = clone(receipt); t7.payload.votes[2].verdict = "maybe";
    await reviewCase(`${label} unknown verdict`, t7, opts);
    const t8 = clone(receipt); t8.payload.type = "match";
    await reviewCase(`${label} not a review`, t8, opts);
    const t9 = clone(receipt); t9.payload.task_sha256 = "0".repeat(64);
    await reviewCase(`${label} payload hash changed (signature breaks)`, t9, opts);
  }
  if (label === "review-3 rotate") {
    const t1 = clone(receipt); t1.payload.reviewers.panel_selection.pool = ["claude-opus-5-5"];
    await reviewCase(`${label} model not in pool`, t1, opts);
    const t2 = clone(receipt); t2.payload.reviewers.panel_selection.mode = "custom";
    await reviewCase(`${label} mode changed`, t2, opts);
  }
  if (label === "review-3 sets") {
    const t1 = clone(receipt); t1.payload.reviewers.panel_selection.options = [opts.panel[0]];
    await reviewCase(`${label} options changed`, t1, opts);
  }
  if (label === "review-3 custom") {
    await reviewCase(`${label} other models asked`, receipt, { ...opts, panel: ["claude-sonnet-5-5", "openai/gpt-6-luna", "google/gemini-3.7-flash"] });
  }
}
// Split and majority-of-cannot_assess cases.
for (const vs of [["pass", "fail", "cannot_assess"], ["pass", "cannot_assess", "cannot_assess"], ["fail", "fail", "fail"]]) {
  const commitment = await review.reviewCommitment("review-3", env);
  const votes = commitment.models.map((m, i) => mkVote(i, m, vs[i]));
  const payload = await review.reviewPayload({ tier: "review-3", input: { task, output }, commitment, votes, payer: "0xabc", network: "base", id: logic.randomToken(12) });
  await reviewCase(`review-3 votes ${vs.join("+")}`, await signed(payload), { task, output });
}

child.kill();
delete out._sk; // the throwaway signing key is never written out; only its public half
out.review_public_key = out._pk;
delete out._pk;
await writeFile(outPath, JSON.stringify(out, null, 1));
console.log(`wrote ${outPath}: ${out.canonical.length} canonical, ${out.randomness.length} randomness, ${out.matches.length} match cases, ${out.series.length} series, ${out.reviews.length} review cases`);
process.exit(0);
