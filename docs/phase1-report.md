# Phase 1 report: one real payout run on Nosana with receipts (2026-11-10)

SPEC_V2 Phase 1, adapted to `docs/POC_PAYOUTS.md`. Everything below comes from one live run,
`runs/payouts-2026-11-10/` (committed: inputs, every job's raw fixtures, the receipts, the verify report,
the ledger tables as JSON). **Phase 1 is done. Phase 2 has not started.**

## Summary

| Acceptance item | Result |
|---|---|
| One real payout run on Nosana | **Done.** 30 synthetic payouts in one batch job (`7f1Br2ta…`, node `5n72gKKf…`), risk-based re-run of 15 on a second node (`5XvbtVDE…`, node `Bvy32h8Q…`) |
| Every anchored receipt passes relay-verify | **Yes: 21 of 21** anchored, all six checks read live from Solana and IPFS. The 9 declared receipts pass schema and id. A tampered output fails check 5 (exit 1) |
| Accuracy with CIs | **Open model: 4/30 = 0.133, Wilson 95% CI [0.053, 0.297].** Claude Code (declared lane, 9 payouts ≤ $500): 9/9, CI [0.701, 1.000]. Frontier ratio: **not measured in POC** (no API credits) |
| The bar (`payouts-need-proof`) | **Not cleared**: CI lower bound 0.053 < 0.90. By the rule (`on_no_open_model: human_review`) the 21 payouts over $500 go to human review, and no anchored receipt carries `work_type_cleared_bar` |
| Credits and timing per job | In the table below. **Phase 1 spend $0.378** of the $3 cap (settled 3.027 → 3.405, nothing reserved) |

The pipeline works end to end, and the receipts check out against the public record. The model doesn't
clear the bar: Qwen2.5-7B, answering straight into guided JSON, gave `hold / seller_on_hold` for all
30 payouts. That is the main finding and the first decision below.

## What was built

| Piece | Files |
|---|---|
| Synthetic payout run + decision ledger, deterministic ground truth | `relay_nosana/payouts/data.py` (seed 20261109; 30 payouts: 13 ok, 4 seller hold, 3 duplicate, 3 amount mismatch, 4 stale price, 3 needs approval; 21 over $500, 5 over $5,000) |
| Task, prompt, output schema, Wilson CI | `relay_nosana/payouts/task.py` |
| Rules | `relay.rules.yaml`, `relay_nosana/rules.py`: `payouts-need-proof` (> $500, anchored, **absolute bar: CI lower ≥ 0.90 vs ground truth**, re-run always > $5,000 plus a 10% sample, at least 15) and `payouts-default` (declared) |
| Batch runner (in the container) | `batch_runner/run.py`: vLLM 0.10.2 by digest, Qwen2.5-7B-Instruct @ `a09a3545…` (model and tokenizer revision), temperature 0, seed 0, guided JSON; prints `RELAY_OUT` rows, a summary, the Merkle root; timing lines; a heartbeat and an in-container deadline |
| Job definitions, pinning, jobs API | `relay_nosana/nosana/{jobdef,ipfs,jobs}.py` (pin to Nosana's community key and our `PINATA_JWT`, both must equal the local CIDv0; results must stay ≤ 256 KiB) |
| Lane with automatic retry | `relay_nosana/lane.py`: post, watch (run account = running), stop in `finally`, fetch the result by CID, accept only a result that passes `check_result`, else retry on another node. Resumable after a crash. Records the node from the run account while a job runs |
| Receipts v0.1 | `relay_verify/{jcs,merkle,outputs,schema,checks}.py`, `relay_nosana/receipts/issue.py`, `docs/receipt-spec.md`, `docs/receipt-schema.json`. JCS (RFC 8785), SHA-256, Merkle with domain separation. Time fields: `listed_at` (on-chain timeStart), `finished_at`, `container_start` / `container_end` (from the result) |
| Verifier CLI | `relay-verify <receipt.json> [--json]`: exit 0 verified, 1 failed, 2 could not check |
| Declared lane | `relay_nosana/payouts/claude_code.py`: headless Claude Code, `claude -p --output-format json --json-schema … --tools "" --no-session-persistence`, one session per payout, from an empty working directory |
| API frontier (kept for later) | `relay_nosana/payouts/frontier.py`, run only with `run_poc frontier --api` |
| Ledger | `relay_nosana/ledger.py`: tables `jobs`, `receipts`, `rules` (plus `calls`) |
| Orchestrator | `relay_nosana/payouts/run_poc.py`: `prepare`, `nosana`, `declared`, `frontier --api`, `issue`, `verify`, `export` |
| Balance units | `providers/nosana.py` already read USD (Phase 0 showed USD is right, HANDOFF's "cents" note was wrong). Docstring and a test added; the arithmetic is unchanged |

**Tests: 151 pass** (36 in `tests/test_receipts.py`: JCS vectors, runner copy parity, Merkle paths and
tamper, data determinism, rules and the absolute bar, job definition size, ledger tables, receipts from
a fake chain verify, five tamper cases, a failed result gives no receipts, network down gives exit 2,
declared receipts with `request_id` or `session_id`, the Claude Code lane's parsing, the run-account node).

## Jobs run (all on the 3090 market `CA5pMpqk…`)

| Job | Tag | Node | Outcome | Post → running | Download | Load (incl. download) | Gen | Listed → finished | Cost |
|---|---|---|---|---|---|---|---|---|---|
| `At7dH6Np…` | batch 1 | `38yH7Chd…` | exit 1: **our bug**, the node altered a raw JSON env value (fixed: base64) | — | — | — | — | 19 s | $0.010 |
| `YYTscXhL…` | batch 2 | `HM7nqCK6…` | **hung**: running 55 min, stopped by us, no result, no logs | 10 s | ? | ? | — | — | $0.161 |
| `BmW33L4Y…` | batch 3 | `38yH7Chd…` | **hung**: same | 3 s | ? | ? | — | — | $0.161 |
| `2gjR5m5B…` | batch 4 | `6YDndB2a…` | exit 1: **node GPU broken** (`NVMLError_Unknown`, "Can't initialize NVML") | — | 116 s | — | — | 147 s | $0.010 |
| `7f1Br2ta…` | **batch 5** | `5n72gKKf…` | **success**, 30 rows | 3 s | 129 s | 148 s | 4.2 s | **173 s** | $0.010 |
| `6Q4QUmz1…` | re-run 1 | `6YDndB2a…` | exit 1: same broken GPU | 67 s | 116 s | — | — | 152 s | $0.010 |
| `5XvbtVDE…` | **re-run 2** | `Bvy32h8Q…` | **success**, 15 rows | 3 s | 179 s | 262 s | 7.1 s | **321 s** | $0.016 |

`import vllm` 9–26 s on every node. Costs are settled credits (balance delta per job, read after
settlement); the reservation is $0.175 per job (1 h × market price). Raw post, account, result, stop
and balance responses for every attempt are in `runs/payouts-2026-11-10/nosana/`.

Node reliability in this run: of 5 nodes, **2 succeeded, 2 hung** for the full 55 min with no result,
**1 had a broken GPU** (twice; it also had a publish failure in Phase 0). The two hangs cost $0.32, 85%
of the Phase 1 spend. Why they hung is unknown: a stopped job publishes nothing. After the hangs the
runner got a heartbeat (stage, elapsed, bytes downloaded) and an in-container deadline (45 min,
exit 3) so a slow node leaves logs, and retries skip nodes that failed or hung in the run. The next two
nodes were fast, so the deadline never fired.

## Accuracy

Ground truth is computed by the deterministic rules in `data.py`, so accuracy is measured, not judged.
From the ledger (`calls`, `phase = payout_check`), Wilson 95% intervals:

| Recipient | Items | Correct | Accuracy | 95% CI |
|---|---|---|---|---|
| `nosana:Qwen/Qwen2.5-7B-Instruct@a09a35458c70` (all payouts) | 30 | 4 | 0.133 | [0.053, 0.297] |
| same, anchored lane only (> $500) | 21 | 3 | 0.143 | [0.050, 0.346] |
| `claude-code:claude-opus-5-5` (≤ $500 only) | 9 | 9 | 1.000 | [0.701, 1.000] |
| Frontier ratio | — | — | not measured in POC | — |

**What the open model did:** `{"decision": "hold", "reason_code": "seller_on_hold"}` for all 30
payouts. The 4 it got right are the 4 seller holds. The prompt lists the rules in priority order with
`seller_on_hold` first, and every payout's input includes the hold decisions (for S-114 and S-207), so
the model has to check the seller id. It never does. With guided JSON and `max_tokens` 48 it has no room
to reason before it commits to `"decision"`. I checked the input and the raw text for a pipeline bug:
the system prompt is applied, the output is exactly the schema, the inputs hash to what the job
definition commits to. I didn't change the prompt or the model after seeing the scores, because that
would be tuning on the test run.

The two lanes aren't comparable as a frontier ratio: different items (30 vs the 9 small ones), and
9 items give a wide interval.

## Re-run on a second node

15 anchored payouts (all 5 over $5,000, plus 10 sampled, seeded by the run id) re-ran on node
`Bvy32h8Q…`: **15 of 15 parsed outputs agree** (`output_sha256`). That is weak evidence here: the model
gave one answer to everything, so agreement was easy. It does show that the same job definition on two
different 3090 nodes gave byte-identical parsed outputs.

## Receipts

- **21 anchored** (P-0101…P-0130 over $500): Merkle path to the batch root `RELAY_MERKLE_ROOT`, job
  `7f1Br2ta…`, node, job definition `QmNYSM6R…`, result `QmXqgLZv…`, inputs CID, `listed_at` /
  `finished_at` / `container_start` / `container_end`, image by digest, weights at the revision, the
  spot check (15 of them). `relay_checks`: `output_schema` and, where re-run, `spot_check_agreed`;
  **no `work_type_cleared_bar`**.
- **9 declared** (≤ $500): `provider = {name: "claude-code", model: "claude-opus-5-5", session_id,
  timestamp}`, `cost_usd` 0 (subscription login; Claude Code's notional list cost for the 9 sessions
  was $0.125, recorded in `declared.jsonl`, not charged). 3.6–12.2 s per payout.
- `relay-verify`: **30 verified, 0 failed, 0 could not check** (`runs/payouts-2026-11-10/verify_report.json`).
  Changing one anchored receipt's output to `approve/ok` and recomputing its hash and id → check 5
  fails ("the Merkle path does not lead to merkle.root"), exit 1.

A receipt proves what ran and what it said, not that it was right: every anchored receipt here
verifies, and 18 of the 21 record a wrong answer. The bar is what stops these payouts, and it did.

## Spend

| | USD |
|---|---|
| Nosana, 7 jobs (settled) | 0.378 |
| Of which the two hung jobs | 0.322 |
| Claude Code declared lane | 0 (subscription; notional $0.125) |
| API frontier | 0 (not run) |
| **Phase 1 total** | **0.378** of the $3 cap |
| Balance now | assigned 10, settled 3.405, reserved 0 → **$6.595 available** |

## What turned out different from the spec

1. **Raw JSON in a job env value was altered on the node** (batch 1 failed to parse the schema). Env
   values that carry JSON are now base64 (`RELAY_OUTPUT_SCHEMA_B64`; the inputs were already gzip +
   base64).
2. **Stopped jobs leave nothing**: no result, no logs, and the job account shows no node (it stays QUEUED
   while running). The node is in the run account at offset 40 while the job runs, and the indexer
   (`/jobs/{address}`) has it afterwards. Added to the runner: heartbeat + deadline, so failures publish.
3. **Node quality varies a lot** inside one market: hangs and a broken GPU on 3 of 5 nodes. Retrying on
   another node, skipping nodes that failed in this run, is necessary, not optional.
4. **The v1 balance was already USD.** HANDOFF's note said cents; Phase 0 showed USD. No arithmetic changed.
5. **The bar changed to absolute** (CI lower ≥ 0.90 against ground truth) because the frontier isn't
   measured without API credits. `bar_cleared` refuses a rule without `ci_lower_gte`.
6. **Declared lane on headless Claude Code** instead of the API: `provider.session_id` replaces
   `request_id` (schema now accepts one of the two). Opus 5.5 on the API also rejects `temperature`, so
   the API lane wouldn't have been temperature 0 either.
7. Download + load was 148–262 s on the two good nodes (Phase 0: 160–1,233 s). Generation for 30
   payouts took 4.2 s. Startup is the cost of a job, not the batch size.

## Decisions needed

1. **The open model fails the bar.** Options, each as a new pre-registered run on a fresh seed (not
   re-scored on this run):
   a. let the model reason before the JSON (a free-text pass, then a guided pass that only formats),
   b. a larger open model that fits a 24 GB card (e.g. Qwen2.5-14B or 32B, AWQ 4-bit), at a pinned revision,
   c. compute the deterministic holds (duplicate, amount mismatch, stale price, refund approval) in
      Relay's check path and leave the model only the parts that need reading (matching a decision's
      text to a payout). In this synthetic task every rule is deterministic, so an investor may ask
      why a model is in the loop at all. That deserves an answer in the demo.
2. **Whether the demo's money shot is the bar refusing**: on this run the gate refuses all 21 large
   payouts with a verified receipt and sends them to human review. That is an honest outcome, but not
   the "approve with proof" story.
3. **Node selection**: keep a per-market allowlist of nodes that succeeded (here `5n72gKKf…`,
   `Bvy32h8Q…`, Phase 0's `2cwhCqyv…`), or keep retrying blind. The hangs were 85% of the spend.
4. **Claude Code as the declared lane** works for the POC. For anything beyond it, a provider call with
   a request id from the API is the stronger declared receipt.

## How to reproduce

```bash
cd ~/relay-nosana && set -a && . ./.env && set +a
.venv/bin/python -m relay_nosana.payouts.run_poc prepare  --run-id payouts-2026-11-10
.venv/bin/python -m relay_nosana.payouts.run_poc nosana   --run-id payouts-2026-11-10 --max-attempts 6
.venv/bin/python -m relay_nosana.payouts.run_poc declared --run-id payouts-2026-11-10
.venv/bin/python -m relay_nosana.payouts.run_poc issue    --run-id payouts-2026-11-10
.venv/bin/python -m relay_nosana.payouts.run_poc verify   --run-id payouts-2026-11-10
.venv/bin/python -m relay_nosana.payouts.run_poc export   --run-id payouts-2026-11-10
.venv/bin/relay-verify runs/payouts-2026-11-10/receipts/P-0101.json
```

`nosana` resumes: a posted attempt is finished from its saved post response, never posted twice.
