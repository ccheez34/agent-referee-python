// A local stand-in for Agent Referee, for the Python client's integration tests. Not shipped in the package.
//
// The match rooms, step acknowledgments, receipts, review payloads and signatures are the REAL server code
// (agent-referee-server/src: match.ts, review.ts, receipt.ts, logic.ts), run in Node with fake AI answers.
// Only the HTTP routing (index.ts uses Hono and x402-hono) is re-created here, from index.ts, and payments are
// emulated: a paid route answers 402 with an x402 v1 body until the request carries an X-PAYMENT header. No real
// money or network is involved.
//
//   node --experimental-strip-types mini_server.mjs SERVER_SRC_DIR
// Prints {"port": N, "public_key": "..."} on its first line, then serves until killed.
import http from "node:http";
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

const src = process.argv[2];
const mod = (f) => import(pathToFileURL(join(src, f)).href);
const { MatchRoom } = await mod("match.ts");
const logic = await mod("logic.ts");
const review = await mod("review.ts");
const { signReceipt } = await mod("receipt.ts");

// A throwaway signing key for this run only.
const kp = await crypto.subtle.generateKey({ name: "Ed25519" }, true, ["sign", "verify"]);
const b64 = (buf) => Buffer.from(buf).toString("base64");
const SIGNING_KEY = b64(await crypto.subtle.exportKey("pkcs8", kp.privateKey));
const PUBLIC_KEY = b64(await crypto.subtle.exportKey("raw", kp.publicKey));

// The judge settings from the server's wrangler.jsonc (its top-level "vars", which the live service deploys with), so
// the stand-in judges as the live service does: Mistral out (Oct 31), judge-3's default closed with two companies, and
// so on. A missing or unreadable file stops the stand-in: silently using other settings is what let them drift (Nov 13).
const JUDGE_KEYS = ["CLAUDE_JUDGES_ENABLED", "JUDGE_MODEL", "JUDGE_MODEL_POOL", "JUDGE_EXCLUDE", "OPT_IN_MODELS", "PANEL_MODELS", "PANEL_2_MODELS", "PANEL_3_MODELS"];
/** JSON as wrangler.jsonc allows it: line and block comments, and trailing commas, outside strings, are dropped. */
export function parseJsonc(text) {
  let out = "", i = 0;
  // Past whitespace and comments from k: the index of the next character that counts.
  const next = (k) => {
    for (;;) {
      while (k < text.length && /\s/.test(text[k])) k++;
      if (text.startsWith("//", k)) { while (k < text.length && text[k] !== "\n") k++; continue; }
      if (text.startsWith("/*", k)) { const e = text.indexOf("*/", k + 2); k = e < 0 ? text.length : e + 2; continue; }
      return k;
    }
  };
  while (i < text.length) {
    const c = text[i];
    if (c === '"') { // a string, copied whole, escapes included
      let j = i + 1;
      while (j < text.length && text[j] !== '"') j += text[j] === "\\" ? 2 : 1;
      out += text.slice(i, j + 1);
      i = j + 1;
    } else if (text.startsWith("//", i) || text.startsWith("/*", i)) {
      i = next(i);
    } else if (c === "," && (text[next(i + 1)] === "}" || text[next(i + 1)] === "]")) {
      i++; // a trailing comma
    } else {
      out += c;
      i++;
    }
  }
  return JSON.parse(out);
}
function judgeSettings(file) {
  let vars;
  try {
    vars = parseJsonc(readFileSync(file, "utf8")).vars;
  } catch (e) {
    console.error(`mini_server: can't read the judge settings from ${file} (${e.message}); set AGENT_REFEREE_SERVER_SRC to the server's src folder`);
    process.exit(2);
  }
  if (!vars || typeof vars !== "object" || typeof vars.JUDGE_MODEL_POOL !== "string") {
    console.error(`mini_server: ${file} has no "vars" with JUDGE_MODEL_POOL`);
    process.exit(2);
  }
  return Object.fromEntries(JUDGE_KEYS.filter((k) => typeof vars[k] === "string").map((k) => [k, vars[k]]));
}
const JUDGE_SETTINGS = judgeSettings(join(src, "..", "wrangler.jsonc"));

const PRICES = { rps: 10000, "rps-bo3": 20000, weighted: 15000, draw: 50000, judge: 100000, "judge-2": 200000, "judge-3": 350000, review: 50000, "review-3": 200000 };

function storage() {
  const m = new Map();
  return {
    get: async (k) => structuredClone(m.get(k)),
    put: async (k, v) => void m.set(k, structuredClone(v)),
    deleteAll: async () => m.clear(),
    setAlarm: async () => {},
    delete: async (k) => m.delete(k),
    list: async () => new Map(m),
  };
}
const rooms = new Map();
const env = {
  STAGING: "true",
  FAKE_AI: "true",
  FAKE_AI_DELAY_MS: "5-20",
  NETWORK: "base-sepolia",
  PAY_TO: "0xF6B53AaCf822A36b7B2aF9C45BabE6E367ede4d3",
  ANTHROPIC_API_KEY: "fake",
  OPENROUTER_API_KEY: "fake",
  SIGNING_KEY,
  PUBLIC_KEY,
  JOIN_WAIT_SECONDS: process.env.JOIN_WAIT_SECONDS || "3",
  // The judge settings: read from the checked-out server's own wrangler.jsonc (below), so they can't drift from the
  // live service's. Without them the code's own defaults are Claude models, which close the judge tiers while Claude
  // judges are off.
  ...JUDGE_SETTINGS,
};
env.MATCHES = {
  idFromName: (n) => n,
  get: (id) => {
    if (!rooms.has(id)) rooms.set(id, new MatchRoom({ storage: storage() }, env));
    const r = rooms.get(id);
    return { fetch: (url, init) => r.fetch(new Request(url, init)) };
  },
};

const forward = async (id, action, body) => {
  const res = await env.MATCHES.get(id).fetch(`https://match/${action}`, {
    method: body === undefined ? "GET" : "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  return { status: res.status, body: await res.json().catch(() => ({})) };
};

// Test switches, set by POST /__test.
const test = { dropSeat: null, busyNextFree: false, freeUsed: new Map() };

const required = (path, amount) => ({
  x402Version: 1,
  error: "X-PAYMENT header is required",
  accepts: [
    {
      scheme: "exact",
      network: "base",
      maxAmountRequired: String(amount),
      resource: `http://127.0.0.1${path}`,
      description: "Agent Referee (test server)",
      mimeType: "application/json",
      payTo: env.PAY_TO,
      maxTimeoutSeconds: path.includes("review") ? 300 : 60,
      asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
      extra: { name: "USD Coin", version: "2" },
    },
  ],
});

const ID_RE = /^[a-f0-9]{24}$/;
const err = (status, error, headers) => ({ status, body: { error }, headers });

async function runReview(tier, body, extra) {
  const v = review.validateReview(body, tier);
  if (!v.ok) return err(400, `${v.error} ${extra.free ? "Your free review wasn't used." : "Nothing was charged."}`);
  let commitment;
  try {
    commitment = await review.reviewCommitment(tier, env, v.value.panel, undefined, v.value.task.length + v.value.output.length);
  } catch (e) {
    return err(400, `${e.message} ${extra.free ? "Your free review wasn't used." : "Nothing was charged."}`);
  }
  if (extra.needPayment) return { status: 402, body: required(`/${tier}`, PRICES[tier]) };
  const keys = { ANTHROPIC_API_KEY: "fake", OPENROUTER_API_KEY: "fake", fake: { minMs: 5, maxMs: 20, mix: [] } };
  const votes = await Promise.all(commitment.models.map((m, i) => review.runOneReviewer(keys, m, i + 1, v.value)));
  const payload = await review.reviewPayload({ tier, input: v.value, commitment, votes, payer: extra.payer, network: env.NETWORK, id: logic.randomToken(12), ...(extra.free ? { funding: "free" } : {}) });
  const receipt = await signReceipt(payload, SIGNING_KEY, PUBLIC_KEY);
  return { status: 200, body: { review_id: payload.review_id, tier, result: payload.result, votes, receipt } };
}

async function route(method, path, headers, body, base) {
  const xp = headers["x-payment"];
  const payer = logic.payerFromPaymentHeader(xp);
  let m;
  if (method === "POST" && path === "/__test") {
    const { resetFree, ...rest } = body ?? {};
    Object.assign(test, rest);
    if (resetFree) test.freeUsed = new Map();
    return { status: 200, body: { ok: true } };
  }
  if (method === "GET" && path === "/pubkey") return { status: 200, body: { algorithm: "Ed25519", public_key: PUBLIC_KEY, history: [] } };
  if (method === "GET" && path === "/reviewers") return { status: 200, body: { long_inputs: review.longReviewRule(env), model_pool: ["claude-opus-5-5"], free: { available: true } } };
  if (method === "POST" && path === "/matches") {
    const input = body;
    const demo = input?.demo === true;
    const id = typeof input?.key === "string" && logic.KEY_RE.test(input.key) ? await logic.idForKey(input.key, demo) : logic.randomToken(12);
    const r = await forward(id, "create", { id, input });
    if (r.status === 201 && input?.host_pays) {
      const t = r.body.invite.invite_token;
      r.body.invite = { ...r.body.invite, url: `${base}/invite/${id}?token=${t}` };
    }
    return r;
  }
  if ((m = path.match(/^\/invite\/([a-f0-9]{24})$/)) && method === "GET") {
    const r = await forward(m[1], "status");
    return r.status === 200 ? { status: 200, body: { match: r.body } } : r;
  }
  if (path === "/invite/join" && method === "POST") return forward(body.match_id, "join", { role: "guest", invite_token: body.invite_token, seat: body.seat });
  if ((m = path.match(/^\/matches\/([a-f0-9]{24})$/)) && method === "GET") return forward(m[1], "status");
  if ((m = path.match(/^\/matches\/([a-f0-9]{24})\/recover$/)) && method === "POST") {
    if (typeof body?.payment !== "string" || body.payment.length < 20) return err(400, "payment must be the exact X-PAYMENT header value you sent when joining.");
    return forward(m[1], "recover", { payment_hash: await logic.sha256Hex(body.payment) });
  }
  if ((m = path.match(/^\/matches\/([a-f0-9]{24})\/(commit|reveal|submit|retry-judge|rematch)$/)) && method === "POST") return forward(m[1], m[2], body ?? {});
  if ((m = path.match(/^(\/demo)?(\/host)?\/join\/(rps|rps-bo3|weighted|judge|judge-2|judge-3|draw\/mediator|draw\/party)$/)) && method === "POST") {
    const demo = !!m[1];
    const hostRoute = !!m[2];
    const tier = m[3].startsWith("draw") ? "draw" : m[3];
    const role = hostRoute ? "host" : m[3] === "draw/mediator" ? "mediator" : "party";
    const paid = m[3] !== "draw/party";
    const charged = paid && !demo;
    if (!body || !ID_RE.test(body.match_id ?? "")) return err(400, "match_id is required.");
    const seat = role === "mediator" ? "mediator" : body.seat;
    if (charged && !xp) return { status: 402, body: required(path, hostRoute ? PRICES[tier] * 2 : PRICES[tier]) };
    const r = await forward(body.match_id, "join", {
      payment_hash: charged ? await logic.sha256Hex(xp) : undefined,
      seat,
      tier,
      role,
      paid: charged,
      payments_enabled: !demo,
      demo,
      wait: paid,
      payer: charged ? payer : undefined,
    });
    if (charged && r.status < 400) {
      r.headers = { "x-payment-response": Buffer.from(JSON.stringify({ success: true, transaction: "0x" + "ab".repeat(32), network: "base", payer })).toString("base64") };
      if (test.dropSeat && test.dropSeat === seat) {
        test.dropSeat = null;
        r.drop = true; // the seat was issued and "paid", but the reply never arrives
      }
    }
    return r;
  }
  if ((m = path.match(/^\/(free\/)?(review|review-3)$/)) && method === "POST") {
    const free = !!m[1];
    const tier = m[2];
    if (free) {
      if (test.busyNextFree) {
        test.busyNextFree = false;
        return { status: 503, body: { error: "busy: the AI reviewers are at capacity. Your free review wasn't used. Nothing was charged; retry in about 7 s.", busy: true, retry_after_s: 7 }, headers: { "retry-after": "7" } };
      }
      const used = test.freeUsed.get(tier) ?? 0;
      const limit = tier === "review-3" ? 1 : 3;
      if (used >= limit) return err(429, `You've used today's free ${tier === "review-3" ? "review-3" : "single-reviewer"} reviews (${limit} per day from one address). They reset at 00:00 UTC.`);
      const r = await runReview(tier, body, { free: true });
      if (r.status === 200) {
        test.freeUsed.set(tier, used + 1);
        r.body.free = { used_today: used + 1, per_day: limit, left_today: limit - used - 1 };
      }
      return r;
    }
    const r = await runReview(tier, body, { needPayment: !xp, payer });
    if (r.status === 200) r.headers = { "x-payment-response": Buffer.from(JSON.stringify({ success: true, transaction: "0x" + "cd".repeat(32), network: "base", payer })).toString("base64") };
    return r;
  }
  return err(404, "Not found. GET / for how to use this service.");
}

const server = http.createServer(async (req, res) => {
  const chunks = [];
  for await (const c of req) chunks.push(c);
  const raw = Buffer.concat(chunks).toString("utf8");
  let body = null;
  try { body = raw ? JSON.parse(raw) : null; } catch {}
  const u = new URL(req.url, "http://127.0.0.1");
  try {
    const r = await route(req.method, u.pathname, req.headers, body, `http://127.0.0.1:${server.address().port}`);
    if (r.drop) return req.socket.destroy();
    res.writeHead(r.status, { "content-type": "application/json", ...(r.headers ?? {}) });
    res.end(JSON.stringify(r.body));
  } catch (e) {
    res.writeHead(500, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: String(e?.stack ?? e) }));
  }
});
server.listen(0, "127.0.0.1", () => console.log(JSON.stringify({ port: server.address().port, public_key: PUBLIC_KEY, judge: JUDGE_SETTINGS })));
