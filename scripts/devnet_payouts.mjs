// Execute a replayed run's payouts on Solana devnet through the gate (Phase 2a, replay mode).
//
//   node scripts/devnet_payouts.mjs payouts-2026-11-10 [--dry-run]
//
// For every payout: verify its receipt with docs/app/verifier.js against mainnet and IPFS, ask the gate
// (docs/app/gate.js), and only on "pay" send one devnet transfer to the seller's devnet address with a Memo
// instruction whose text is the receipt_id. Writes docs/app/runs/<run>/payouts.json. Idempotent: an item that
// already has a confirmed tx is not paid again.
//
// The payer is a devnet-only keypair in out/devnet/payer.json (gitignored). Devnet SOL has no value.
// Amounts: 1 USD cent = 1,000 lamports (a $250 payout sends 0.025 devnet SOL).

import { createPrivateKey, createPublicKey, sign, createHash } from "node:crypto";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import { HttpFetcher, verify, verdict, b58encode, b58decode, VERDICTS } from "../docs/app/verifier.js";
import { decide } from "../docs/app/gate.js";

const DEVNET = "https://api.devnet.solana.com";
export const MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr";
const SYSTEM_PROGRAM = "11111111111111111111111111111111";
const LAMPORTS_PER_CENT = 1000;

const runId = process.argv[2];
const dry = process.argv.includes("--dry-run");
const base = new URL(`../docs/app/runs/${runId}/`, import.meta.url);
const run = JSON.parse(readFileSync(new URL("run.json", base)));

function keyFromSeed(seed) {
  const priv = createPrivateKey({ key: Buffer.concat([Buffer.from("302e020100300506032b657004220420", "hex"), seed]),
    format: "der", type: "pkcs8" });
  return { priv, pub: new Uint8Array(createPublicKey(priv).export({ format: "der", type: "spki" }).subarray(12)) };
}
// A seller's devnet address: a public key derived from the seller id. Nobody needs its private key.
export const sellerAddress = (sellerId) =>
  b58encode(keyFromSeed(createHash("sha256").update(`relay-poc devnet seller ${sellerId}`).digest()).pub);

async function rpc(method, params) {
  for (let i = 0; i < 5; i++) {
    const r = await fetch(DEVNET, { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }) });
    if (r.status === 429 || r.status >= 500) { await new Promise((s) => setTimeout(s, 2000 * (i + 1))); continue; }
    const j = await r.json();
    if (j.error) throw new Error(`${method}: ${JSON.stringify(j.error)}`);
    return j.result;
  }
  throw new Error(`${method}: devnet RPC unavailable`);
}

const shortvec = (n) => { const o = []; for (;;) { let b = n & 0x7f; n >>= 7; if (n) o.push(b | 0x80); else { o.push(b); return o; } } };
function u64le(n) { const b = Buffer.alloc(8); b.writeBigUInt64LE(BigInt(n)); return [...b]; }

// A legacy transaction: payer signs; SystemProgram.transfer(payer -> seller), Memo(receipt_id) signed by payer.
function buildTx(payer, to, lamports, memo, blockhash) {
  const keys = [payer.pub, b58decode(to), b58decode(SYSTEM_PROGRAM), b58decode(MEMO_PROGRAM)];
  const transfer = [2, 0, 0, 0, ...u64le(lamports)];
  const memoBytes = [...Buffer.from(memo, "utf8")];
  const msg = Buffer.from([
    1, 0, 2, ...shortvec(keys.length), ...keys.flatMap((k) => [...k]), ...b58decode(blockhash),
    ...shortvec(2),
    2, ...shortvec(2), 0, 1, ...shortvec(transfer.length), ...transfer,
    3, ...shortvec(1), 0, ...shortvec(memoBytes.length), ...memoBytes,
  ]);
  const sig = sign(null, msg, payer.priv);
  return { sig: b58encode(sig), wire: Buffer.concat([Buffer.from([1]), sig, msg]).toString("base64") };
}

async function confirm(sig) {
  for (let i = 0; i < 40; i++) {
    const st = (await rpc("getSignatureStatuses", [[sig], { searchTransactionHistory: true }])).value[0];
    if (st && st.err) throw new Error(`${sig} failed: ${JSON.stringify(st.err)}`);
    if (st && ["confirmed", "finalized"].includes(st.confirmationStatus)) return st;
    await new Promise((s) => setTimeout(s, 1500));
  }
  throw new Error(`${sig} not confirmed`);
}

const out = existsSync(new URL("payouts.json", base)) ? JSON.parse(readFileSync(new URL("payouts.json", base))) : null;
const prior = new Map((out?.items || []).filter((x) => x.tx).map((x) => [x.payout_id, x]));
const payer = keyFromSeed(Buffer.from(JSON.parse(readFileSync(new URL("../out/devnet/payer.json", import.meta.url))).seed_b64, "base64"));
const payerAddr = b58encode(payer.pub);
const rules = Object.fromEntries(run.rules.map((r) => [r.id, r]));
const f = new HttpFetcher();
const items = [];
for (const p of run.payouts) {
  const receipt = p.receipt ? JSON.parse(readFileSync(new URL(p.receipt, base))) : null;
  let v = null;
  if (receipt) v = verdict(await verify(receipt, f));
  const d = decide(receipt, v, rules[p.lane], run.bar);
  const item = { payout_id: p.payout_id, seller_id: p.seller_id, amount_usd: p.amount_usd, lane: p.lane,
    receipt_id: receipt?.receipt_id ?? null, grade: receipt?.grade ?? null,
    verify: v === null ? null : VERDICTS[v], action: d.action, reasons: d.reasons, tx: null };
  if (d.action === "pay") {
    item.to = sellerAddress(p.seller_id);
    item.lamports = Math.round(p.amount_usd * 100) * LAMPORTS_PER_CENT;
    if (prior.has(p.payout_id) && prior.get(p.payout_id).receipt_id === item.receipt_id) item.tx = prior.get(p.payout_id).tx;
    else if (!dry) {
      const { value: bh } = await rpc("getLatestBlockhash", [{ commitment: "confirmed" }]);
      const tx = buildTx(payer, item.to, item.lamports, item.receipt_id, bh.blockhash);
      const sig = await rpc("sendTransaction", [tx.wire, { encoding: "base64", preflightCommitment: "confirmed" }]);
      if (sig !== tx.sig) throw new Error(`devnet returned ${sig}, expected ${tx.sig}`);
      await confirm(sig);
      item.tx = sig;
    }
  }
  items.push(item);
  console.log(`${p.payout_id} $${p.amount_usd} ${p.lane} ${item.verify ?? "no receipt"} -> ${d.action}${item.tx ? " " + item.tx : ""}`);
}
const count = (a) => items.filter((x) => x.action === a).length;
const doc = { run_id: runId, cluster: "devnet", rpc: DEVNET, payer: payerAddr, memo_program: MEMO_PROGRAM,
  lamports_per_cent: LAMPORTS_PER_CENT, executed_at: dry ? null : new Date().toISOString(), dry_run: dry,
  verified_with: { rpc: f.rpc, gateways: f.gateways },
  summary: { pay: count("pay"), paid: items.filter((x) => x.tx).length, hold: count("hold"),
    human_review: count("human_review"), refuse: count("refuse") }, items };
writeFileSync(new URL("payouts.json", base), JSON.stringify(doc, null, 1) + "\n");
console.log(doc.summary);
