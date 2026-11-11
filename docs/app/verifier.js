// relay-verify in the browser (and Node >= 18): a port of relay_verify/ for receipt v0.2.
// No dependencies. Reads Solana JSON-RPC and IPFS gateways directly; trusts nothing from Relay.
// Must give the same verdict and the same check results as the Python CLI on the same receipt:
// tests/parity/ runs both over recorded fixtures (passing receipts and every tamper).

export const NOSANA_JOBS_PROGRAM = "nosJhNRqr2bc9g1nfGDcXXTXvYUmxD4cVwy2pMWhrYM";
// The public mainnet RPC answers browser requests (any Origin header) with 403, so the browser default
// is PublicNode, which allows any origin. Old receipts need an archival RPC (decided in Phase 2).
export const DEFAULT_RPC = "https://solana-rpc.publicnode.com";
export const DEFAULT_GATEWAYS = ["https://nosana.mypinata.cloud/ipfs/", "https://gateway.pinata.cloud/ipfs/",
  "https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"];
export const VERSION = "0.2";

const LIST = [54, 174, 193, 67, 17, 41, 132, 38];
const ASSIGN = [73, 66, 125, 203, 81, 41, 64, 135];
const FINISH = [67, 74, 170, 132, 125, 233, 182, 37];
const JOB_ACCOUNT = [91, 16, 162, 5, 45, 210, 125, 65];
const POST_MARKET = 1, ASSIGN_NODE = 3, FINISH_MARKET = 2, FINISH_NODE = 8;
const STATES = { 0: "QUEUED", 1: "RUNNING", 2: "COMPLETED", 3: "STOPPED" };
const enc = new TextEncoder();

// ---------------------------------------------------------------- base58, hex, sha256

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";
export function b58encode(bytes) {
  let n = 0n;
  for (const b of bytes) n = n * 256n + BigInt(b);
  let s = "";
  while (n > 0n) { s = B58[Number(n % 58n)] + s; n /= 58n; }
  for (const b of bytes) { if (b !== 0) break; s = "1" + s; }
  return s;
}
export function b58decode(s) {
  let n = 0n;
  for (const c of s) {
    const i = B58.indexOf(c);
    if (i < 0) throw new Error(`invalid base58 character: ${c}`);
    n = n * 58n + BigInt(i);
  }
  const out = [];
  while (n > 0n) { out.unshift(Number(n & 255n)); n >>= 8n; }
  for (const c of s) { if (c !== "1") break; out.unshift(0); }
  return Uint8Array.from(out);
}
export const hex = (b) => Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
export const unhex = (h) => Uint8Array.from(h.match(/../g) || [], (x) => parseInt(x, 16));
const cat = (...parts) => {
  const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
  let o = 0;
  for (const p of parts) { out.set(p, o); o += p.length; }
  return out;
};
const eq = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
export async function sha256(bytes) {
  return new Uint8Array(await globalThis.crypto.subtle.digest("SHA-256", bytes));
}

// ---------------------------------------------------------------- JCS (RFC 8785)

function ser(v) {
  if (v === null) return "null";
  if (v === true) return "true";
  if (v === false) return "false";
  if (typeof v === "number") {
    if (!Number.isFinite(v)) throw new Error("NaN and Infinity are not JSON");
    if (Number.isInteger(v) && Math.abs(v) > 2 ** 53) throw new Error(`integer ${v} outside ±2**53`);
    return Object.is(v, -0) ? "0" : String(v);
  }
  if (typeof v === "string") return JSON.stringify(v);
  if (Array.isArray(v)) return "[" + v.map(ser).join(",") + "]";
  if (typeof v === "object") {
    const keys = Object.keys(v).sort((a, b) => (a < b ? -1 : a > b ? 1 : 0)); // UTF-16 code units
    return "{" + keys.map((k) => JSON.stringify(k) + ":" + ser(v[k])).join(",") + "}";
  }
  throw new Error(`not JSON: ${typeof v}`);
}
export const canonical = (obj) => enc.encode(ser(obj));
export const sha256Hex = async (obj) => hex(await sha256(canonical(obj)));

// ---------------------------------------------------------------- CIDv0 (UnixFS, kubo defaults)

const CHUNK = 262144, MAX_LINKS = 174;
function varint(n) {
  const out = [];
  for (;;) { const b = n & 0x7f; n = Math.floor(n / 128); if (n) out.push(b | 0x80); else { out.push(b); return Uint8Array.from(out); } }
}
const fBytes = (f, d) => cat(varint((f << 3) | 2), varint(d.length), d);
const fVar = (f, v) => cat(varint(f << 3), varint(v));
function fileBlock(data) {
  let u = fVar(1, 2);
  if (data.length) u = cat(u, fBytes(2, data));
  return fBytes(1, cat(u, fVar(3, data.length)));
}
export const digestToCid = (d) => b58encode(cat(Uint8Array.from([0x12, 0x20]), d));
export async function cidv0(data) {
  if (data.length <= CHUNK) return digestToCid(await sha256(fileBlock(data)));
  const chunks = [];
  for (let i = 0; i < data.length; i += CHUNK) chunks.push(data.subarray(i, i + CHUNK));
  if (chunks.length > MAX_LINKS) throw new Error("needs more than one DAG level; not supported");
  let links = new Uint8Array(), sizes = new Uint8Array();
  for (const ch of chunks) {
    const leaf = fileBlock(ch);
    const link = cat(fBytes(1, cat(Uint8Array.from([0x12, 0x20]), await sha256(leaf))), fBytes(2, new Uint8Array()),
      fVar(3, leaf.length));
    links = cat(links, fBytes(2, link));
    sizes = cat(sizes, fVar(4, ch.length));
  }
  return digestToCid(await sha256(cat(links, fBytes(1, cat(fVar(1, 2), fVar(3, data.length), sizes)))));
}

// ---------------------------------------------------------------- Merkle

export async function leafHash(itemId, inSha, outSha) {
  return sha256(cat(Uint8Array.from([0]), enc.encode(itemId), unhex(inSha), unhex(outSha)));
}
const nodeHash = (l, r) => sha256(cat(Uint8Array.from([1]), l, r));
export async function merkleRoot(rows) {
  const sorted = [...rows].sort((a, b) => (a.item_id < b.item_id ? -1 : a.item_id > b.item_id ? 1 : 0));
  let level = [];
  for (const r of sorted) level.push(await leafHash(r.item_id, r.input_sha256, r.output_sha256));
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i + 1 < level.length; i += 2) next.push(await nodeHash(level[i], level[i + 1]));
    if (level.length % 2) next.push(level[level.length - 1]);
    level = next;
  }
  return hex(level[0]);
}
export async function rootFromPath(leaf, path) {
  let h = leaf;
  for (const s of path) {
    const sib = unhex(s.hash);
    if (s.side === "L") h = await nodeHash(sib, h);
    else if (s.side === "R") h = await nodeHash(h, sib);
    else throw new Error(`bad side ${s.side}`);
  }
  return hex(h);
}

// ---------------------------------------------------------------- result and outputs

export function checkResult(result) {
  const reasons = [];
  if (result.status !== "success") reasons.push(`result status is '${result.status}', not 'success'`);
  const errors = result.errors || [];
  if (errors.length) reasons.push(`result lists ${errors.length} error(s): ${String(errors[0]).slice(0, 200)}`);
  const ops = result.opStates || [];
  if (!ops.length) reasons.push("result has no opStates: no container ran");
  for (const op of ops) {
    if (op.exitCode !== 0) reasons.push(`op '${op.operationId}' exit code ${op.exitCode}`);
    if (op.status !== "success") reasons.push(`op '${op.operationId}' status '${op.status}'`);
  }
  return { ok: !reasons.length, reasons };
}
export function parseOutputs(result) {
  const text = (result.opStates || []).flatMap((op) => (op.logs || []).filter((e) => e.type === "stdout")
    .map((e) => e.log || "")).join("");
  const rows = {};
  let summary = null;
  for (const line of text.split(/\r\n|\r|\n/)) {
    if (line.startsWith("RELAY_OUT ")) {
      const row = JSON.parse(line.slice(10));
      if (row.item_id in rows) throw new Error(`item ${row.item_id} appears twice in the result`);
      rows[row.item_id] = row;
    } else if (line.startsWith("RELAY_SUMMARY ")) summary = JSON.parse(line.slice(14));
  }
  return [rows, summary];
}
export function regexResult(result, name) {
  for (const op of result.opStates || []) {
    const v = (op.results || {})[name];
    if (Array.isArray(v) && v.length >= 2) return v[1];
  }
  return null;
}
export function containerTimes(result) {
  const ops = result.opStates || [];
  return ops.length ? [ops[0].startTime ?? null, ops[0].endTime ?? null] : [null, null];
}

// ---------------------------------------------------------------- Solana: job account and transactions

function u64le(b, o) { let n = 0n; for (let i = 7; i >= 0; i--) n = n * 256n + BigInt(b[o + i]); return n; }
function i64le(b, o) { const n = u64le(b, o); return Number(n >= 2n ** 63n ? n - 2n ** 64n : n); }
const ZERO32 = new Uint8Array(32);

export function decodeJobAccount(address, data) {
  if (data.length < 233) throw new Error(`job account data is ${data.length} bytes, expected 233`);
  if (!eq(data.subarray(0, 8), JOB_ACCOUNT)) throw new Error("not a Nosana job account (discriminator mismatch)");
  const cid = (b) => (eq(b, ZERO32) ? null : digestToCid(b));
  const k = (o) => data.subarray(o, o + 32);
  const state = data[208];
  return {
    address, ipfs_job: cid(k(8)), ipfs_result: cid(k(40)), market: b58encode(k(72)), node: b58encode(k(104)),
    payer: b58encode(k(136)), price: Number(u64le(data, 168)), project: b58encode(k(176)), state,
    state_name: STATES[state] || `UNKNOWN(${state})`, time_end: i64le(data, 209), time_start: i64le(data, 217),
    timeout: i64le(data, 225),
  };
}
export function jobFromRpcResponse(address, resp) {
  const value = (resp.result || {}).value;
  if (value == null) return null;
  if (value.owner !== NOSANA_JOBS_PROGRAM) throw new Error(`account ${address} is owned by ${value.owner}, not the Nosana jobs program`);
  return decodeJobAccount(address, Uint8Array.from(atob(value.data[0]), (c) => c.charCodeAt(0)));
}

export class TxMismatch extends Error {}
function txKeys(tx) {
  const msg = tx.transaction.message;
  const la = (tx.meta || {}).loadedAddresses || {};
  return [[...msg.accountKeys, ...(la.writable || []), ...(la.readonly || [])],
    new Set(msg.accountKeys.slice(0, msg.header.numRequiredSignatures))];
}
function nosanaIxs(tx) {
  const [keys] = txKeys(tx);
  let ixs = [...tx.transaction.message.instructions];
  for (const inner of (tx.meta || {}).innerInstructions || []) ixs = ixs.concat(inner.instructions || []);
  return ixs.filter((ix) => keys[ix.programIdIndex] === NOSANA_JOBS_PROGRAM)
    .map((ix) => [b58decode(ix.data), ix.accounts.map((i) => keys[i])]);
}
function txOk(tx, sig, what) {
  if (tx == null) throw new TxMismatch(`${what} transaction ${sig} not found`);
  if ((tx.transaction.signatures || [null])[0] !== sig) throw new TxMismatch(`the RPC returned a different transaction for ${what} ${sig}`);
  if ((tx.meta || {}).err != null) throw new TxMismatch(`${what} transaction ${sig} failed: ${JSON.stringify(tx.meta.err)}`);
  return tx;
}
const disc = (d, x) => eq(d.subarray(0, 8), x);
export function decodePostTx(tx, sig, job) {
  txOk(tx, sig, "post");
  const [, signers] = txKeys(tx);
  for (const [d, accts] of nosanaIxs(tx)) {
    if (!accts.length || accts[0] !== job) continue;
    const name = disc(d, LIST) ? "list" : disc(d, ASSIGN) ? "assign" : null;
    if (!name || d.length < 48) continue;
    if (!signers.has(job)) throw new TxMismatch(`post transaction ${sig} is not signed by the job keypair ${job}`);
    return { ix: name, ipfs_job: digestToCid(d.subarray(8, 40)), timeout: i64le(d, 40), market: accts[POST_MARKET],
      node: name === "assign" ? accts[ASSIGN_NODE] : null, listed_at: tx.blockTime };
  }
  throw new TxMismatch(`post transaction ${sig} has no Nosana list or assign instruction for job ${job}`);
}
export function decodeFinishTx(tx, sig, job) {
  txOk(tx, sig, "finish");
  const [, signers] = txKeys(tx);
  for (const [d, accts] of nosanaIxs(tx)) {
    if (!accts.length || accts[0] !== job || !disc(d, FINISH) || d.length < 40) continue;
    const node = accts[FINISH_NODE];
    if (!signers.has(node)) throw new TxMismatch(`finish transaction ${sig}: node ${node} did not sign it`);
    return { ipfs_result: digestToCid(d.subarray(8, 40)), market: accts[FINISH_MARKET], node, finished_at: tx.blockTime };
  }
  throw new TxMismatch(`finish transaction ${sig} has no Nosana finish instruction for job ${job}`);
}
export function jobFromTxs(job, postSig, post, finSig, fin) {
  const p = decodePostTx(post, postSig, job), f = decodeFinishTx(fin, finSig, job);
  if (p.market !== f.market) throw new TxMismatch(`post names market ${p.market}, finish names ${f.market}`);
  if (p.node !== null && p.node !== f.node) throw new TxMismatch(`assign names node ${p.node}, but ${f.node} signed the finish`);
  return { job, post_tx: postSig, finish_tx: finSig, post_ix: p.ix, ipfs_job: p.ipfs_job, ipfs_result: f.ipfs_result,
    market: p.market, node: f.node, listed_at: p.listed_at, finished_at: f.finished_at };
}

// ---------------------------------------------------------------- schema (mirrors relay_verify/schema.py)

const HEX64 = /^[0-9a-f]{64}$/, CID = /^Qm[1-9A-HJ-NP-Za-km-z]{44}$/, B58RE = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/,
  SIG = /^[1-9A-HJ-NP-Za-km-z]{64,90}$/;
const isInt = (v) => typeof v === "number" && Number.isInteger(v);
const T = { str: (v) => typeof v === "string", obj: (v) => v !== null && typeof v === "object" && !Array.isArray(v),
  list: Array.isArray, num: (v) => typeof v === "number", int: isInt };
const COMMON = { receipt_version: "str", grade: "str", receipt_id: "str", item_id: "str", source: "obj", rule: "obj",
  input_sha256: "str", output: "obj", output_sha256: "str", relay_checks: "list", cost_usd: "num" };
const NOSANA = { job: B58RE, market: B58RE, node: B58RE, job_definition_cid: CID, result_cid: CID, inputs_cid: CID,
  post_tx: SIG, finish_tx: SIG, listed_at: "int", finished_at: "int", container_start: "int", container_end: "int" };

export function validate(r) {
  const errs = [];
  const need = (o, spec, where) => {
    for (const [k, t] of Object.entries(spec)) {
      if (!(k in o)) errs.push(`${where}${k} missing`);
      else if (t instanceof RegExp) { if (typeof o[k] !== "string" || !t.test(o[k])) errs.push(`${where}${k} malformed`); }
      else if (!T[t](o[k])) errs.push(`${where}${k} has the wrong type`);
    }
  };
  need(r, COMMON, "");
  if (errs.length) return errs;
  if (r.receipt_version !== VERSION) errs.push(`receipt_version is '${r.receipt_version}', not '${VERSION}'`
    + (r.receipt_version === "0.1" ? " (v0.1 anchored on the job account, which Nosana closes: re-issue as v0.2)" : ""));
  for (const k of ["receipt_id", "input_sha256", "output_sha256"]) if (!HEX64.test(r[k])) errs.push(`${k} is not 64 hex chars`);
  need(r.source, { vendor: "str", work_type: "str" }, "source.");
  need(r.rule, { id: "str", requires: "str" }, "rule.");
  if (r.grade === "anchored") {
    need(r, { merkle: "obj", nosana: "obj", model: "obj", spot_check: "obj" }, "");
    if (errs.length) return errs;
    need(r.nosana, NOSANA, "nosana.");
    need(r.merkle, { root: "str", path: "list" }, "merkle.");
    need(r.model, { image: "str", weights: "str" }, "model.");
    if (!errs.length) {
      if (!HEX64.test(r.merkle.root)) errs.push("merkle.root is not 64 hex chars");
      for (const s of r.merkle.path) {
        if (!T.obj(s) || !["L", "R"].includes(s.side) || !HEX64.test(String(s.hash ?? ""))) { errs.push("merkle.path entry malformed"); break; }
      }
      if (!r.model.image.includes("@sha256:")) errs.push("model.image is not pinned by digest");
      if (!r.model.weights.includes("@")) errs.push("model.weights has no @revision");
      errs.push(...spotCheckErrs(r.spot_check));
    }
  } else if (r.grade === "declared") {
    need(r, { provider: "obj" }, "");
    if (!errs.length) {
      need(r.provider, { name: "str", model: "str", timestamp: "str" }, "provider.");
      if (!["request_id", "session_id"].some((k) => typeof r.provider[k] === "string")) errs.push("provider has neither request_id nor session_id");
    }
    for (const k of ["nosana", "merkle"]) if (k in r) errs.push(`declared receipt carries ${k}`);
  } else errs.push(`unknown grade '${r.grade}'`);
  return errs;
}
function spotCheckErrs(sc) {
  const errs = ["rerun_job", "rerun_node", "agreed", "action"].filter((k) => !(k in sc)).map((k) => `spot_check.${k} missing`);
  if (errs.length) return errs;
  if (![true, false, null].includes(sc.agreed)) errs.push("spot_check.agreed is not true, false or null");
  if (!["none", "human_review", "pending_rerun"].includes(sc.action)) errs.push("spot_check.action is not one of ('none', 'human_review', 'pending_rerun')");
  if (sc.action === "pending_rerun" && (sc.agreed !== null || !sc.reason)) errs.push("spot_check pending_rerun must have agreed null and a reason");
  if (sc.agreed === false) {
    if (sc.action !== "human_review") errs.push("spot_check disagreed but action is not human_review");
    if (typeof sc.reason !== "string" || !sc.reason) errs.push("spot_check disagreed but gives no reason");
    if (!HEX64.test(String(sc.rerun_output_sha256 ?? ""))) errs.push("spot_check disagreed but has no rerun_output_sha256");
  }
  return errs;
}

export async function receiptIdOf(r) {
  const { receipt_id, ...rest } = r;
  return sha256Hex(rest);
}

// ---------------------------------------------------------------- fetchers

export class Unavailable extends Error {}

export class HttpFetcher {
  constructor({ rpc = DEFAULT_RPC, gateways = DEFAULT_GATEWAYS, timeoutMs = 60000, onFetch = null } = {}) {
    Object.assign(this, { rpc, gateways, timeoutMs, onFetch });
    this._tx = new Map(); this._acct = new Map(); this._ipfs = new Map();
  }
  async _rpc(method, params) {
    let last;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const r = await fetch(this.rpc, { method: "POST", headers: { "content-type": "application/json" },
          body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }), signal: AbortSignal.timeout(this.timeoutMs) });
        if (r.status === 429 || r.status >= 500) throw new Error(`HTTP ${r.status}`);
        const j = await r.json();
        if (j.error) throw new Error(`${method}: ${JSON.stringify(j.error)}`);
        this.onFetch?.({ kind: "rpc", method, params });
        return j;
      } catch (e) { last = e; await new Promise((s) => setTimeout(s, 1500 * (attempt + 1))); }
    }
    throw new Unavailable(`Solana RPC ${this.rpc}: ${last}`);
  }
  async transaction(sig) {
    if (!this._tx.has(sig)) this._tx.set(sig, (await this._rpc("getTransaction", [sig, { encoding: "json",
      maxSupportedTransactionVersion: 0, commitment: "confirmed" }])).result ?? null);
    return this._tx.get(sig);
  }
  async account(address) {
    if (!this._acct.has(address)) {
      const resp = await this._rpc("getAccountInfo", [address, { encoding: "base64", commitment: "confirmed" }]);
      this._acct.set(address, jobFromRpcResponse(address, resp));
    }
    return this._acct.get(address);
  }
  async ipfs(cid) {
    if (!this._ipfs.has(cid)) {
      const notes = [];
      for (const gw of this.gateways) {
        let data;
        try {
          const r = await fetch(gw + cid, { signal: AbortSignal.timeout(this.timeoutMs) });
          if (!r.ok) { notes.push(`${gw}: HTTP ${r.status}`); continue; }
          data = new Uint8Array(await r.arrayBuffer());
        } catch (e) { notes.push(`${gw}: ${e}`); continue; }
        if (await cidv0(data) === cid) { this._ipfs.set(cid, data); this.onFetch?.({ kind: "ipfs", cid, gateway: gw }); break; }
        notes.push(`${gw}: bytes do not hash to ${cid}`);
      }
      if (!this._ipfs.has(cid)) throw new Unavailable(`IPFS ${cid}: ${notes.join("; ")}`);
    }
    return this._ipfs.get(cid);
  }
}

// ---------------------------------------------------------------- the checks

async function gunzip(bytes) {
  const s = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  return new Uint8Array(await new Response(s).arrayBuffer());
}
const dec = new TextDecoder();
const json = (bytes) => JSON.parse(dec.decode(bytes));

export async function verify(receipt, f) {
  const checks = [];
  const add = (id, name, ok, reason = "") => checks.push({ id, name, ok, reason });
  const errs = validate(receipt);
  add("schema", "receipt has the v0.2 shape", !errs.length, errs.join("; "));
  if (errs.length) return checks;
  const rid = await receiptIdOf(receipt);
  add("receipt_id", "receipt_id is the hash of the receipt", rid === receipt.receipt_id,
    rid === receipt.receipt_id ? "" : `computed ${rid}`);
  if (receipt.grade === "declared") {
    const p = receipt.provider;
    add("declared", "declared: not verifiable against a public record", true,
      `provider ${p.name} says model ${p.model} ran ` + ("request_id" in p ? `(request ${p.request_id})` : `(session ${p.session_id})`));
    return checks;
  }
  const n = receipt.nosana, m = receipt.merkle, mdl = receipt.model;
  let chain = null, jd = null, result = null;

  const name1 = "job finished on Solana";
  try {
    const post = await f.transaction(n.post_tx), fin = await f.transaction(n.finish_tx);
    const missing = [["post_tx", post], ["finish_tx", fin]].filter(([, t]) => t == null).map(([w]) => w);
    if (missing.length) {
      add("1", name1, null, `RPC ${f.rpc} has no record of ${missing.join(" and ")}. Verifying a receipt older than the `
        + "RPC's history needs an archival RPC (--rpc or RELAY_VERIFY_RPC)");
    } else {
      chain = jobFromTxs(n.job, n.post_tx, post, n.finish_tx, fin);
      result = json(await f.ipfs(chain.ipfs_result));
      const bad = [...checkResult(result).reasons];
      const acct = await f.account(n.job);
      let note;
      if (acct == null) note = "job account closed by Nosana after the job; checked from its transactions";
      else {
        note = "job account still open and agrees with the transactions";
        if (acct.state !== 2) bad.push(`job account state is ${acct.state_name}`);
        for (const k of ["ipfs_job", "ipfs_result", "node", "market"]) {
          if (acct[k] !== chain[k]) bad.push(`job account ${k} is ${acct[k]}, transactions say ${chain[k]}`);
        }
      }
      add("1", name1, !bad.length, bad.length ? bad.join("; ") : note);
    }
  } catch (e) {
    if (e instanceof TxMismatch) add("1", name1, false, e.message);
    else if (e instanceof Unavailable) add("1", name1, null, e.message);
    else throw e;
  }

  try {
    jd = json(await f.ipfs(n.job_definition_cid));
    if (chain === null) {
      const c1 = checks.find((c) => c.id === "1").ok;
      add("2", "job definition is the one the job ran", c1 === null ? null : false, "no post transaction to compare with");
    } else {
      const ok = chain.ipfs_job === n.job_definition_cid;
      add("2", "job definition is the one the job ran", ok, ok ? "" : `the post transaction lists ipfsJob ${chain.ipfs_job}`);
    }
  } catch (e) {
    if (!(e instanceof Unavailable)) throw e;
    add("2", "job definition is the one the job ran", null, e.message);
  }

  if (jd === null) add("3", "image and model revision match", null, "job definition unavailable");
  else {
    const args = jd.ops[0].args, env = args.env || {};
    const weights = `${env.MODEL ?? "None"}@${env.REVISION ?? "None"}`;
    const bad = [];
    if (args.image !== mdl.image) bad.push(`job definition image is ${args.image}`);
    if (weights !== mdl.weights) bad.push(`job definition weights are ${weights}`);
    add("3", "image and model revision match", !bad.length, bad.join("; "));
  }

  if (jd === null) add("4", "input is in the job's inputs", null, "job definition unavailable");
  else {
    const bad = [];
    const metaCid = ((jd.meta || {}).relay || {}).inputs_cid;
    if (metaCid !== n.inputs_cid) bad.push(`job definition names inputs ${metaCid}`);
    const env = jd.ops[0].args.env || {};
    const raw = await gunzip(Uint8Array.from(atob(env.RELAY_INPUTS_GZ_B64), (c) => c.charCodeAt(0)));
    if (await cidv0(raw) !== n.inputs_cid) bad.push("embedded inputs do not hash to inputs_cid");
    const lines = dec.decode(raw).split(/\r\n|\r|\n/).filter((x) => x.trim()).map((x) => JSON.parse(x));
    const line = lines.find((x) => x.item_id === receipt.item_id);
    if (!line) bad.push(`item ${receipt.item_id} is not in the inputs`);
    else if (await sha256Hex(line) !== receipt.input_sha256) bad.push("the item's input does not hash to input_sha256");
    add("4", "input is in the job's inputs", !bad.length, bad.join("; "));
  }

  if (chain === null || result === null) {
    add("5", "output is in the job's result", chain === null ? null : false, "result unavailable");
  } else {
    const bad = [];
    if (chain.ipfs_result !== n.result_cid) bad.push(`the finish transaction's ipfsResult is ${chain.ipfs_result}`);
    const [rows, summary] = parseOutputs(result);
    const row = rows[receipt.item_id];
    if (await sha256Hex(receipt.output) !== receipt.output_sha256) bad.push("output does not hash to output_sha256");
    if (!row) bad.push(`item ${receipt.item_id} is not in the result`);
    else {
      if (row.output_sha256 !== receipt.output_sha256) bad.push("the result's output for this item differs");
      if (row.input_sha256 !== receipt.input_sha256) bad.push("the result's input hash for this item differs");
    }
    const leaf = await leafHash(receipt.item_id, receipt.input_sha256, receipt.output_sha256);
    if (await rootFromPath(leaf, m.path) !== m.root) bad.push("the Merkle path does not lead to merkle.root");
    const all = Object.values(rows);
    const roots = { summary: (summary || {}).merkle_root ?? null, regex: regexResult(result, "merkle_root"),
      recomputed: all.length ? await merkleRoot(all) : null };
    for (const [k, v] of Object.entries(roots)) if (v !== m.root) bad.push(`the result's ${k} root is ${v}`);
    add("5", "output is in the job's result", !bad.length, bad.join("; "));
  }

  if (chain === null) add("6", "node and times match", null, "no transactions");
  else {
    const bad = [];
    for (const k of ["node", "market", "listed_at", "finished_at"]) if (n[k] !== chain[k]) bad.push(`${k}: the transactions say ${chain[k]}`);
    if (result !== null) {
      const [cs, ce] = containerTimes(result);
      if (n.container_start !== cs || n.container_end !== ce) bad.push(`container times: result says ${cs}..${ce}`);
    }
    add("6", "node and times match", !bad.length, bad.join("; "));
  }
  return checks;
}

export function verdict(checks) {
  if (checks.some((c) => c.ok === false)) return 1;
  if (checks.some((c) => c.ok === null)) return 2;
  return 0;
}
export const VERDICTS = ["verified", "failed", "could not check"];
