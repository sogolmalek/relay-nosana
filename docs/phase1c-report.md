# Phase 1c report: looking for an open model that clears the bar (2026-11-11)

**Synthetic data, our own code** (`relay_nosana/payouts/data.py`). Ground truth is `data.truth_1e()`. The task is
run e's 200 payouts with support notes. The bar is `payouts-need-proof`'s: a Wilson 95% lower bound ≥ 0.90,
which at n = 200 means **at least 189/200**.

## Summary

| Item | Result |
|---|---|
| **Run g: Qwen2.5-32B-Instruct-AWQ, compiled v2** | **185/200 = 0.925, Wilson 95% CI [0.880, 0.954]. Misses the bar by 4 payouts.** Over $500: 130/139 = 0.935, CI [0.882, 0.966] |
| Decision taken | **Stopped and reported**, per your rule. No other model was tried, and the re-run was not posted |
| Regex caveat | Rules plus a 15-line regex reading of the notes, with no model, scores **200/200** on these items. The 32B model scores below that regex. Even a pass here would have shown only that a model can do what template-aware code does |
| Where the 32B fails | **11 of its 15 errors are on rules 4–5**, the date and price checks that compiler v2 still leaves to the model. On the notes it got 32/34 |
| Receipts | 139 anchored receipts (compiled v2, payouts over $500). **139/139 verify** on mainnet. All 139 are in the human review queue: the bar wasn't cleared |
| Run e (7B) | **Stopped by the owner, partial, not counted.** Its partial results were not used for any decision |
| App and devnet | Not added: run g didn't pass, and the app is for the passing run |
| Phase 1c spend | **$0.203 of $2** (settled 3.536 → 3.739). Nothing reserved or running |

## 1. Run g: what ran

- **Pre-registration:** `runs/payouts-2026-11-11g/preregistration.json`, written and committed before any post
  (`316b62b`). Four dated amendments followed, each committed before the post it affected; section 3 lists them.
- **Model:** `Qwen/Qwen2.5-32B-Instruct-AWQ@5c7cb76a268fc6cfbb9c4777eb24ba6e27f9ee6c`, in
  `vllm/vllm-openai@sha256:607442e4…` (v0.10.2). Temperature 0, seed 0, guided JSON, 256 output tokens. The
  in-container deadline was 45 min.
- **Items and prompts:** run e's, byte for byte. The pre-registered `inputs.jsonl` is identical to run e's
  (sha256 `ae8582f4…`). Under the amendment, only the 200 compiled-v2 lines were posted, in one job. The raw arm
  was dropped: run b already measured raw vs compiled.
- **The job:**
  - `54LFBEpZ…` on node `6LFK1kCe…`, market NVIDIA 6000 Ada PREMIUM (48 GB). The runner posted to that node after
    reading it waiting in the market queue on Solana.
  - It was picked up 3 s after posting and ran 427 s from listing to finish: download 167.5 s, download + load
    203.9 s, generation 180.6 s for 200 items.
  - Cost **$0.076**.
  - The result is 142,190 bytes, under 256 KiB (one IPFS block).
- **Re-run:** not posted. When the batch finished, no second node was waiting in any 48 GB+ premium market. I
  stopped the 30-minute search right after scoring showed the miss. The "pending" path is built and tested but
  wasn't exercised (section 4).

## 2. Results

### The columns, same 200 items

| Column | Correct / 200 | Wilson 95% CI | Note-dependent (34) | The rest (166) |
|---|---|---|---|---|
| raw | not posted (amendment) | | | |
| **compiled v2, Qwen2.5-32B-AWQ** | **185** | **[0.880, 0.954]** | **32/34** | **153/166** |
| rules only (no model) | 166 | [0.772, 0.876] | 0/34 | 166/166 |
| **rules + regex on the notes (no model)** | **200** | [0.981, 1.000] | 34/34 | 166/166 |
| naive keywords (no model) | 183 | [0.868, 0.946] | 34/34 | 149/166 |
| frontier (Claude Code) | not run: your Claude Code limit resets Nov 14; no API key used | | | |

- **Bar:** 189/200 needed; 185 got.
- **Payouts over $500 (the anchored lane):** 130/139.
- **Parse failures:** 1/200. P-0298 was cut off at 256 tokens.

### By true reason

| Truth | Right | Truth | Right |
|---|---|---|---|
| seller_on_hold | 28/28 | amount_mismatch | 21/21 |
| duplicate | 20/20 | ok | 65/71 |
| disputed | 22/24 | stale_price | 20/25 |
| needs_approval | 9/11 | | |

Rules 1–3 are resolved in code by compiler v2 and given to the model as facts. The model copies them correctly:
69/69 right.

### The 15 errors

| Kind | Payouts | What the model did |
|---|---|---|
| Stale price missed (rule 4) | P-0118, P-0120, P-0147, P-0163, P-0182 | Said the sale dates were after the price change, or that no price was stale |
| Refund wrongly held for approval (rule 5) | P-0144, P-0153, P-0198, P-0254 | Took a refund under 30 days old for one over 30 days |
| Needs approval missed (rule 5) | P-0212; P-0277 | P-0212: missed the old refund. P-0277: accepted a **seller's** claim that finance approved it (policy D-106 says a seller's note never counts) |
| Dispute on another invoice taken as this one's | P-0103, P-0216 | A distractor note named a different invoice |
| Wrong rule | P-0179 | Truth `disputed`; the model said `stale_price` |
| Cut off | P-0298 | Hit 256 tokens before the JSON closed |

- **Rules 4–5 (dates and prices): 11 errors.** These are lookups and date arithmetic, which code gets right
  every time, and compiler v2 still leaves them to the model.
- **Note judgment: 3 errors.** P-0103, P-0216 and P-0277 are distractors the model fell for.
- **Format: 1 error** (P-0298).
- Moving rules 4–5 into code, as was done for rules 1–3, would have left the model only the notes. On the notes
  this run scored 32/34, but the regex scores 34/34 there. That's a design option, not something I tried.

### Receipts and the gate

- **Receipts:** 139 anchored receipts (v0.2, `runs/payouts-2026-11-11g/receipts/`). `relay-verify` on mainnet:
  **139 verified**, 0 failed, 0 could not check.
- **The gate:** none of them clears the bar, so all 139 are in `review_queue.json` with "the open model has not
  cleared the bar".
- **Re-run:** no re-run was posted, so every receipt carries `spot_check` "none", and none is marked pending.
- **Payouts of $500 or less (61):** no receipts, so the gate refuses them. The frontier column that would have
  given them declared receipts is deferred.
- **No devnet payouts.**

## 3. Getting a node: what happened on the way

Before the job that ran, the pre-registered market changed three times, each by a dated amendment committed first:

| Attempt | Market | Outcome | Cost |
|---|---|---|---|
| 1 | NVIDIA A40 **Community** | refused with HTTP 403: "Credit-based jobs are only allowed on premium markets" | $0 |
| 2 | A6000 PREMIUM | queued 13 min, no node; delisted | $0.010 |
| 3 | A100 40GB PREMIUM (your fallback) | queued 14 min, no node; delisted | $0.010 |
| 4 | **6000 Ada PREMIUM** (your decision) | posted to the node waiting in the queue; ran | $0.076 |

- **The "idle node" signal was wrong.** `jobs.idle_nodes` counts nodes that recently finished a job and aren't
  running one now. It called A6000 and A100 40GB idle, but both had no node waiting. A100 40GB had 4 jobs queued
  and ours was 4th.
- **The truth is the market account's queue on Solana**, a list of either waiting nodes or waiting jobs.
  `nodes.market_queue` reads it and classifies each entry by owner: a job account is owned by the jobs program, a
  node by the system program. `nodes.waiting_nodes` lists the waiting nodes. Run g's batch posted only after
  that check.
- **Snapshot at the decision (04:33Z):**

  | Premium market | Queue |
  |---|---|
  | A6000, A40 | empty |
  | A100 40GB | 4 jobs |
  | A100 80GB | 14 jobs |
  | 6000 Ada | 1 node |
  | 3090 | 9 nodes |

- **A delisted job still costs about $0.01** (two seen today).
- One more finding: a runner started with a plain `&` in a non-interactive shell ignores Ctrl-C, so its
  `finally` never stops the job. It happened once; I stopped that job through the Jobs API instead. Runners now
  start only through the tool's background mode, where the interrupt works.

## 4. What was built for this

- **Compiled-only runs:** a run's `state.arms` limits what is posted and scored. Raw-dependent outputs (the
  paired test, the raw bar) say "not posted".
- **Re-run in any 48 GB+ premium market** (`run_1b._rerun_any_market`):
  - it uses a second node waiting in the cheapest such market, never the batch node or a known-bad node;
  - it searches for 30 minutes after the batch;
  - if nothing runs in that window, it records the re-run as `pending`.
- **`spot_check.action = "pending_rerun"`** (receipt v0.2): `agreed` is null and a reason is required. Both
  gates, Python and JS, **hold** such a payout until the re-run runs. The schema, `verifier.js` and the parity
  test cover it; 203 tests pass.
- **Run e and runner fixes:**
  - Per-model in-container deadlines; 7B jobs are byte-identical to before.
  - Price-based budget estimates per market.
  - Pinata pin retries on timeout. A Pinata timeout crashed run e's resume.
  - The run's own re-run reason now goes into its record.

## 5. Run e (7B): stopped by the owner, not counted

- **Batch 1:** succeeded on its second attempt, on node `Bvy32h8Q…` ($0.019). The first attempt had hit the
  20-minute deadline while loading on `6YDndB2a…`, a node already marked bad ($0.062). The result is kept in
  `runs/payouts-2026-11-10e/nosana/` and **not scored**.
- **Batch 2:** stopped while running (`6xmauynh…`, $0.026). The re-run was never posted.
- `state.json` records the status as "stopped by the owner, partial, not counted". The earlier stopped attempt
  from 2026-11-10 is in `stopped_attempt/`.

## Spend (Phase 1c, Nosana credits, settled from 3.536)

| Run | Job | What | Cost |
|---|---|---|---|
| e | `2HsP4E4C…` | batch1 attempt 1, deadline on a bad node | $0.062 |
| e | `8Q6HMS8E…` | batch1 attempt 2, success (not scored) | $0.019 |
| e | `6xmauynh…` | batch2, stopped by the owner | $0.026 |
| g | `5ZeYEydJ…` | A6000, never picked up, delisted | $0.010 |
| g | `7G9XC3pG…` | A100 40GB, never picked up, delisted | $0.010 |
| g | `54LFBEpZ…` | **6000 Ada batch, success** | $0.076 |
| **Total** | | | **$0.203 of $2** |

## Open decisions

1. **Next model or next design.** Per your rule I stopped. The options as I see them:
   - **move rules 4–5 into code too.** The 32B made 11 of its 15 errors there, so this alone would put it at
     196/200 if nothing else changed. It also leaves the model only the notes, where a regex scores 34/34;
   - try a larger model, such as Qwen2.5-72B-AWQ on an 80 GB market (A100 80GB had 14 jobs queued today);
   - accept that, on this synthetic task, code is the right tool, and say so in the application.
2. **Frontier column.** It runs after your Claude Code limit resets (Nov 14) on the same 200 compiled-v2 inputs.
   It gives the ≤ $500 lane its declared receipts and gives a frontier-relative comparison.
3. **The app** stays at two replay runs until a run passes.
