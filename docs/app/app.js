// The POC app in replay mode: real committed runs, verified live from the browser against Solana and IPFS.
import { HttpFetcher, verify, verdict, VERDICTS, receiptIdOf, sha256Hex, b58decode, DEFAULT_RPC } from "./verifier.js";
import { decide } from "./gate.js";

const DEVNET_RPC = "https://api.devnet.solana.com";
const MEMO_PROGRAM = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr";
const SYSTEM_PROGRAM = "11111111111111111111111111111111";
const $ = (s) => document.querySelector(s);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v; else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined) e.setAttribute(k, v);
  }
  for (const k of kids.flat()) if (k !== null && k !== undefined) e.append(k.nodeType ? k : String(k));
  return e;
};
const short = (s, n = 6) => (s && s.length > 2 * n + 1 ? `${s.slice(0, n)}…${s.slice(-n)}` : s);
const money = (x) => "$" + x.toLocaleString("en-US", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
const when = (t) => new Date(t * 1000).toISOString().replace("T", " ").replace(".000Z", " UTC");
const links = {
  tx: (s, devnet) => `https://explorer.solana.com/tx/${s}${devnet ? "?cluster=devnet" : ""}`,
  addr: (a, devnet) => `https://explorer.solana.com/address/${a}${devnet ? "?cluster=devnet" : ""}`,
  ipfs: (c) => `https://nosana.mypinata.cloud/ipfs/${c}`,
};
function hash(value, href) {
  const copy = el("button", { class: "copy", title: "copy", "aria-label": "copy", onclick: () => navigator.clipboard?.writeText(value) }, "⧉");
  return el("span", { class: "h" }, href ? el("a", { href, target: "_blank", rel: "noopener", title: value }, short(value, 8)) : short(value, 8), copy);
}
const pill = (text, kind = "") => el("span", { class: `pill ${kind}` }, text);
const ACTION = { pay: ["pay", "good"], hold: ["hold", "warn"], human_review: ["human review", "warn"], refuse: ["refused", "bad"],
  pay_after_verify: ["pays once verified", "acc"] };

const S = { run: null, base: "", receipts: {}, verdicts: {}, payouts: null, selected: null, fetcher: null };

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(`${url}: HTTP ${r.status}`);
  return r.json();
}

async function init() {
  const index = await getJSON("runs/index.json");
  const sel = $("#run");
  for (const r of index) sel.append(el("option", { value: r.run_id }, `${r.title} (${r.date})`));
  const want = new URLSearchParams(location.search).get("run");
  if (want && index.some((r) => r.run_id === want)) sel.value = want;
  sel.addEventListener("change", () => { history.replaceState(null, "", `?run=${sel.value}`); load(sel.value); });
  $("#rpc-name").textContent = new URL(DEFAULT_RPC).host;
  await load(sel.value);
}

async function load(runId) {
  S.base = `runs/${runId}/`;
  S.run = await getJSON(S.base + "run.json");
  S.receipts = {};
  S.verdicts = {};
  S.selected = null;
  S.fetcher = new HttpFetcher();
  await Promise.all(S.run.payouts.filter((p) => p.receipt).map(async (p) => { S.receipts[p.payout_id] = await getJSON(S.base + p.receipt); }));
  try { S.payouts = await getJSON(S.base + "payouts.json"); } catch { S.payouts = null; }
  $("#foot-receipts").href = `https://github.com/sogolmalek/relay-nosana/tree/main/runs/${runId}/receipts`;
  renderAll();
}

function renderAll() {
  const R = S.run;
  $("#banner").replaceChildren(el("b", {}, "Replay of a real run"), ` from ${R.date} (${R.run_id}). `,
    "The jobs, transactions, results and receipts are real and checked live from your browser. The payout data is synthetic. ",
    "Running a new job on Nosana from this page comes in the next step.");
  $("#run-title").textContent = `${R.title}: ${R.design}`;
  if (R.caveat) $("#banner").append(el("div", { class: "small", style: "margin-top:6px" }, el("b", {}, "Caveat: "), R.caveat));
  $("#decisions").replaceChildren(...R.decisions.map((d) => el("li", {}, el("b", {}, d.id), ` (${d.date}) ${d.text}`)));
  $("#rules").replaceChildren(...R.rules.map((r) => el("div", { class: "small", style: "margin-bottom:8px" },
    el("b", {}, r.id), r.when ? ` · when amount > $${r.when.amount_usd_gt}` : " · otherwise", ` · requires ${r.requires}`,
    r.bar ? ` · bar: Wilson 95% lower bound ≥ ${r.bar.ci_lower_gte}` : "", r.on_no_open_model ? ` · if no open model clears it: ${r.on_no_open_model}` : "")));
  const anchored = R.payouts.filter((p) => p.lane === "payouts-need-proof"), rest = R.payouts.filter((p) => p.lane !== "payouts-need-proof");
  $("#route").replaceChildren(
    stat(R.payouts.length, "payouts in the run"),
    stat(anchored.length, "over $500: Nosana batch, anchored receipts"),
    stat(rest.length, `$500 or less: ${rest.some((p) => p.receipt) ? "Claude Code, declared receipts" : "no receipts issued in this run"}`),
    stat(R.rerun_items.length, "re-run on a second node"));
  renderJobs();
  renderReceipts();
  renderQueue();
  renderPayouts();
  $("#s6").hidden = true;
}
const stat = (num, lab) => el("div", {}, el("div", { class: "num" }, num), el("div", { class: "lab" }, lab));

function renderJobs() {
  const R = S.run;
  $("#jobs").replaceChildren(...R.jobs.map((j) => el("div", { class: "job" },
    el("h3", {}, j.tag === "rerun" ? "Re-run on a second node" : j.tag === "batch" ? "Batch job" : `Batch job, shard ${j.tag.slice(5)}`, " ", pill(j.post_ix === "assign" ? "assigned to a node" : "listed on the market", "acc")),
    el("ul", { class: "timeline" },
      li("job definition pinned", hash(j.job_definition_cid, links.ipfs(j.job_definition_cid))),
      li("posted on Solana", hash(j.post_tx, links.tx(j.post_tx)), ` · ${when(j.listed_at)}`),
      li("job account", hash(j.job, links.addr(j.job)), " (closed by Nosana after the job)"),
      li("ran on node", hash(j.node, links.addr(j.node))),
      li("finished, result pinned", hash(j.finish_tx, links.tx(j.finish_tx)), " → ", hash(j.result_cid, links.ipfs(j.result_cid)), ` · ${when(j.finished_at)}`),
      li("listed → finished", `${j.finished_at - j.listed_at} s`, j.cost_usd !== null && j.cost_usd !== undefined ? ` · cost $${j.cost_usd.toFixed(3)}` : "")))));
  const m = R.bar.measured;
  $("#jobs").append(el("div", { class: "small muted" }, `Model: ${R.model.weights} in ${short(R.model.image, 22)}. `,
    typeof R.rerun_agreement === "string" ? `Re-run: ${R.rerun_agreement}.` : `Re-run agreement: ${R.rerun_agreement.agreed} of ${R.rerun_agreement.n}.`));
}
const li = (k, ...v) => el("li", {}, el("span", { class: "k" }, k), el("span", {}, ...v));

function gateFor(p) {
  const rules = Object.fromEntries(S.run.rules.map((r) => [r.id, r]));
  const r = S.receipts[p.payout_id];
  const v = r ? (p.payout_id in S.verdicts ? S.verdicts[p.payout_id] : null) : null;
  return decide(r || null, v, rules[p.lane], S.run.bar);
}

function renderReceipts() {
  const tb = $("#receipts tbody");
  tb.replaceChildren(...S.run.payouts.map((p) => {
    const r = S.receipts[p.payout_id];
    const out = r ? r.output : p.open_model_output;
    const right = out && out.decision === p.truth.decision && out.reason_code === p.truth.reason_code;
    const v = S.verdicts[p.payout_id];
    const g = gateFor(p);
    return el("tr", { "data-payout": p.payout_id, class: S.selected === p.payout_id ? "sel" : "" },
      el("td", {}, p.payout_id), el("td", { class: "r" }, money(p.amount_usd)),
      el("td", {}, out ? `${out.decision} / ${out.reason_code}` : "–", " ", pill(right ? "right" : "wrong", right ? "" : "")),
      el("td", { class: "muted" }, `${p.truth.decision} / ${p.truth.reason_code}`),
      el("td", {}, r ? pill(r.grade, r.grade === "anchored" ? "acc" : "") : el("span", { class: "muted" }, "no receipt")),
      el("td", { "data-testid": `verdict-${p.payout_id}` }, v === undefined ? (r ? el("span", { class: "muted" }, "not yet") : "–")
        : pill(VERDICTS[v], v === 0 ? "good" : "bad")),
      el("td", {}, pill(...ACTION[g.action])),
      el("td", {}, r ? el("button", { class: "link", "data-testid": `open-${p.payout_id}`, onclick: () => openReceipt(p.payout_id) }, "open") : ""));
  }));
}

async function verifyAll() {
  const btn = $("#verify-all"), st = $("#verify-all-status");
  btn.disabled = true;
  const ids = Object.keys(S.receipts);
  let done = 0;
  for (const id of ids) {
    st.textContent = `checking ${id} (${done}/${ids.length})…`;
    S.verdicts[id] = verdict(await verify(S.receipts[id], S.fetcher));
    done++;
    renderReceipts();
  }
  const ok = ids.filter((i) => S.verdicts[i] === 0).length;
  st.textContent = `${ok} of ${ids.length} receipts verified in this browser`;
  btn.disabled = false;
  renderQueue();
  renderPayouts();
}

function openReceipt(id) {
  S.selected = id;
  renderReceipts();
  const r = S.receipts[id], p = S.run.payouts.find((x) => x.payout_id === id);
  $("#s6").hidden = false;
  $("#detail-title").textContent = `${id}: ${money(p.amount_usd)} to ${p.seller_id}, ${r.grade} receipt`;
  $("#receipt-json").href = S.base + p.receipt;
  $("#checks").replaceChildren();
  $("#evidence").replaceChildren();
  $("#tamper-out").replaceChildren();
  renderTampers(r, p);
  $("#s6").scrollIntoView({ behavior: "smooth", block: "start" });
}

function checkList(checks) {
  return checks.map((c) => el("li", { "data-check": c.id, "data-ok": String(c.ok) },
    pill(c.ok === true ? "pass" : c.ok === false ? "fail" : "n/a", c.ok === true ? "good" : c.ok === false ? "bad" : ""),
    el("div", {}, el("div", {}, el("b", {}, c.id), " ", c.name), c.reason ? el("div", { class: "why" }, c.reason) : "")));
}

async function verifyOne() {
  const id = S.selected, r = S.receipts[id];
  const ev = $("#evidence");
  ev.replaceChildren(el("div", {}, "Reading from Solana and IPFS…"));
  const log = [];
  const f = new HttpFetcher({ onFetch: (x) => log.push(x) });
  const checks = await verify(r, f);
  S.verdicts[id] = verdict(checks);
  $("#checks").replaceChildren(...checkList(checks));
  ev.replaceChildren(el("div", {}, el("b", {}, `Verdict: ${VERDICTS[S.verdicts[id]]}. `), `Fetched by this browser: ${log.length} reads.`),
    ...log.map((x) => el("div", {}, x.kind === "rpc" ? `Solana ${x.method} ${short(String(x.params[0]), 10)} via ${new URL(f.rpc).host}`
      : `IPFS ${short(x.cid, 10)} from ${new URL(x.gateway).host}, bytes hashed back to the CID`)));
  renderReceipts();
  renderQueue();
  renderPayouts();
}

// The tamper buttons mirror tests/parity/cases.py: the forger recomputes receipt_id (except "output only").
function tampers(r, p) {
  const other = S.run.tamper_other || S.run.jobs.find((j) => j.tag === "rerun");
  const T = [];
  if (r.grade === "anchored") {
    T.push(["Change the decision", "5", (x) => {
      const flip = x.output.decision === "hold" ? "approve" : "hold";
      x.output = { decision: flip, reason_code: flip === "approve" ? "ok" : x.output.reason_code };
      return sha256Hex(x.output).then((h) => { x.output_sha256 = h; });
    }]);
    T.push(["Change the decision, leave the id", "receipt_id", (x) => { x.output = { decision: "approve", reason_code: "ok" }; return "keep-id"; }]);
    T.push(["Change the model", "3", (x) => { x.model.weights = x.model.weights.split("@")[0] + "@" + "0".repeat(40); }]);
    T.push(["Swap the job", "1", (x) => { x.nosana.job = other.job; }]);
    T.push(["Point at another job's post", "1", (x) => { x.nosana.post_tx = other.post_tx; }]);
    T.push(["Edit a Merkle hash", "5", (x) => { x.merkle.path[0].hash = "ab".repeat(32); }]);
    T.push(["Change the node", "6", (x) => { x.nosana.node = other.node !== x.nosana.node ? other.node : S.run.jobs[0].node; }]);
  } else {
    T.push(["Change the decision", "receipt_id", (x) => { x.output = { decision: x.output.decision === "hold" ? "approve" : "hold", reason_code: "ok" }; return "keep-id"; }]);
    T.push([`Use this declared receipt for a $2,400 payout`, "gate", (x) => {
      const proof = S.run.rules.find((y) => y.id === "payouts-need-proof");
      x.rule = { id: proof.id, requires: proof.requires, snapshot_sha256: x.rule.snapshot_sha256 };
    }]);
  }
  return T;
}

function renderTampers(r, p) {
  $("#tampers").replaceChildren(...tampers(r, p).map(([label, expect, fn]) => el("button", {
    "data-testid": `tamper-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/-$/, "")}`, "data-expect": expect,
    onclick: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true;
      const x = structuredClone(r);
      const keep = (await fn(x)) === "keep-id";
      if (!keep) x.receipt_id = await receiptIdOf(x);
      const checks = await verify(x, S.fetcher);
      const v = verdict(checks);
      const rules = Object.fromEntries(S.run.rules.map((y) => [y.id, y]));
      const g = decide(x, v, rules[x.rule.id], S.run.bar);
      const failing = checks.filter((c) => c.ok === false).map((c) => c.id);
      $("#tamper-out").replaceChildren(el("div", { class: "tout", "data-testid": "tamper-result", "data-failing": failing.join(","), "data-gate": g.action },
        el("div", {}, el("b", {}, label), ": ", pill(VERDICTS[v], v === 0 ? "good" : "bad"),
          failing.length ? ` check ${failing.join(", ")} failed` : " every check passes", " · gate: ", pill(...ACTION[g.action])),
        g.reasons.length ? el("div", { class: "small muted" }, g.reasons.join("; ")) : "",
        el("ul", { class: "checks" }, ...checkList(checks.filter((c) => c.ok !== true)))));
      btn.disabled = false;
    } }, label)));
}

function renderQueue() {
  const R = S.run, m = R.bar.measured;
  $("#bar-note").replaceChildren(el("b", {}, "Payouts over $500: held: the open model missed the bar. "),
    `${m.model} got ${m.correct}/${m.n} = ${m.accuracy.toFixed(3)} against the ground truth (Wilson 95% CI ${m.ci_low.toFixed(3)}–${m.ci_high.toFixed(3)}); `,
    `on the payouts over $500 alone, ${m.over_500.correct}/${m.over_500.n}. The rule needs a lower bound of at least ${R.bar.needed_ci_low.toFixed(2)}, `,
    "and on_no_open_model is human review, never a silent fallback. The receipts are genuine; the model isn't good enough yet.");
  const q = R.payouts.map((p) => [p, gateFor(p)]).filter(([, g]) => g.action === "human_review");
  q.sort((a, b) => (b[1].reasons.length - a[1].reasons.length) || a[0].payout_id.localeCompare(b[0].payout_id));
  $("#queue").replaceChildren(el("div", { class: "small muted", style: "margin-bottom:8px" }, `${q.length} payouts in the queue.`),
    ...q.map(([p, g]) => {
      const r = S.receipts[p.payout_id];
      const flag = r?.spot_check?.agreed === false;
      return el("div", { class: `q ${flag ? "flag" : ""}`, "data-payout": p.payout_id, "data-testid": `queue-${p.payout_id}` },
        el("div", {}, el("b", {}, p.payout_id), ` · ${money(p.amount_usd)} · model said ${r.output.decision}/${r.output.reason_code}`,
          flag ? [" ", pill("re-run disagreed", "bad")] : ""),
        flag ? el("div", { class: "small" }, `Batch node ${short(r.nosana.node)} said ${r.output.decision}/${r.output.reason_code}; `,
          `the re-run on ${short(r.spot_check.rerun_node)} said ${p.rerun_output.decision}/${p.rerun_output.reason_code}. `,
          `Ground truth: ${p.truth.decision}/${p.truth.reason_code}. Same input, temperature 0.`) : "",
        el("ul", { class: "small" }, ...g.reasons.map((x) => el("li", {}, x))));
    }));
}

function renderPayouts() {
  const P = S.payouts;
  const tb = $("#payouts tbody");
  if (!P) {
    $("#pay-summary").replaceChildren();
    tb.replaceChildren(el("tr", {}, el("td", { colspan: 5, class: "muted" }, "No devnet payouts recorded for this run yet.")));
    $("#check-payouts").disabled = true;
    return;
  }
  $("#check-payouts").disabled = !P.items.some((x) => x.tx);
  $("#pay-summary").replaceChildren(stat(P.summary.paid, "paid on devnet"), stat(P.summary.hold, "held: the model said hold"),
    stat(P.summary.human_review, "human review"), stat(P.summary.refuse, "refused (no valid receipt)"));
  tb.replaceChildren(...P.items.map((x) => el("tr", { "data-payout": x.payout_id },
    el("td", {}, x.payout_id), el("td", { class: "r" }, money(x.amount_usd)), el("td", {}, pill(...ACTION[x.action])),
    el("td", { class: "small" }, x.reasons.join("; ") || (x.tx ? `verified ${x.grade} receipt, approve` : "")),
    el("td", { "data-testid": `devnet-${x.payout_id}` }, x.tx ? hash(x.tx, links.tx(x.tx, true)) : (x.action === "pay" ? "not sent yet" : "–")))));
  const when_ = P.executed_at ? `executed ${P.executed_at.replace("T", " ").slice(0, 19)} UTC` : "dry run only: nothing sent";
  $("#payouts-status").textContent = `Recorded run, ${when_}, from payer ${short(P.payer)}.`;
}

async function devnetTx(sig) {
  const r = await fetch(DEVNET_RPC, { method: "POST", headers: { "content-type": "application/json" },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "getTransaction", params: [sig, { encoding: "json", maxSupportedTransactionVersion: 0 }] }) });
  return (await r.json()).result;
}

async function checkPayouts() {
  const P = S.payouts, st = $("#payouts-status");
  $("#check-payouts").disabled = true;
  let ok = 0, n = 0;
  for (const x of P.items.filter((y) => y.tx)) {
    n++;
    const cell = document.querySelector(`[data-testid="devnet-${x.payout_id}"]`);
    st.textContent = `checking ${x.payout_id} on devnet…`;
    const bad = [];
    const tx = await devnetTx(x.tx);
    if (!tx) bad.push("not found on devnet");
    else {
      const keys = tx.transaction.message.accountKeys;
      if (tx.meta.err) bad.push("transaction failed");
      const memo = tx.transaction.message.instructions.find((i) => keys[i.programIdIndex] === MEMO_PROGRAM);
      const memoText = memo ? new TextDecoder().decode(b58decode(memo.data)) : null;
      if (memoText !== x.receipt_id) bad.push(`memo is ${memoText}`);
      const tr = tx.transaction.message.instructions.find((i) => keys[i.programIdIndex] === SYSTEM_PROGRAM);
      if (!tr || keys[tr.accounts[1]] !== x.to) bad.push("transfer goes elsewhere");
      const r = S.receipts[x.payout_id];
      if (!r || r.receipt_id !== x.receipt_id) bad.push("memo names a different receipt");
      else {
        if (!(x.payout_id in S.verdicts)) S.verdicts[x.payout_id] = verdict(await verify(r, S.fetcher));
        if (S.verdicts[x.payout_id] !== 0) bad.push("its receipt does not verify");
        if (r.output.decision !== "approve") bad.push("its receipt says hold");
      }
    }
    if (!bad.length) ok++;
    cell.append(" ", pill(bad.length ? bad.join("; ") : "memo = receipt_id, receipt verified", bad.length ? "bad" : "good"));
  }
  st.textContent = `${ok} of ${n} devnet payouts checked in this browser: each memo is the receipt_id of a verified approve receipt.`;
  renderReceipts();
}

$("#verify-all").addEventListener("click", verifyAll);
$("#verify-one").addEventListener("click", verifyOne);
$("#check-payouts").addEventListener("click", checkPayouts);
init().catch((e) => { $("#banner").textContent = `Could not load the run: ${e}`; });
