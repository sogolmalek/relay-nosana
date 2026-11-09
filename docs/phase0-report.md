# Phase 0 report: day-one checks against the live Nosana API (2026-11-09)

SPEC_V2 section 5. Everything below comes from live runs. Raw responses are in
`tests/fixtures/phase0/`. **Phase 0 is done. Phase 1 has not started.**

## Summary

The verifiable lane works on Nosana with credits. Three things in `docs/phase0-research.md` were
wrong against the live API and are corrected there:

1. **Pinning:** nodes fetch job definitions only through `nosana.mypinata.cloud`, which serves only
   CIDs pinned to **Nosana's** Pinata account. We pin to Nosana's account with the community key that
   `@nosana/ipfs` publishes as intentionally public, and to our own account for durability.
2. **Balance units:** the balance is in **USD**, not cents.
3. **Job state:** an on-chain state of **COMPLETED does not mean success.** The job account also
   stays QUEUED while a node runs it, and `timeStart` is the listing time.

Spend: **at most $0.33** of real credit (settled $0.159, plus $0.175 still reserved for the last
job). The script's reservation-based tally in `spend.json` is $0.818. The cap was $3.

## Checks

| # | Check (SPEC_V2 §5) | Result | Notes / workaround |
|---|---|---|---|
| 0 | Key and base URL | **Works** | `POST /auth/validate-api-key` and `GET /credits/balance` answer 200 on both `client-manager.k8s.prd.nosana.com` and `api.nosana.com`. The earlier 401 was the old key. |
| 0b | Balance units | **Research was wrong** | USD: `assignedCredits: 10` = $10. A $0.044 reservation moved `reservedCredits` by 0.044. `credits.creditsUsed` in the post response is in thousandths of a dollar (44 = $0.044). v1 `providers/nosana.py` reading dollars is correct. |
| 1 | Pinning | **Works, with a workaround** | Pinning only with our `PINATA_JWT` makes the job fail on the node: 403 "owner of this gateway does not have this content pinned" (hello attempt 1). With Nosana's community key, `pinFileToIPFS` is allowed, and the returned CID **equals our local CIDv0** every time (4 job definitions). Our own pin returns the same CID. The key is read at runtime from `.nosana-npm/node_modules/@nosana/ipfs` or `NOSANA_IPFS_JWT` and is never logged or committed. |
| 2 | Post a credit job with our own definition | **Works** | `POST /jobs/list` → `{job, tx, run, credits}`. The 1-hour minimum timeout is reserved up front ($0.044 on the 3060 market, $0.175 on the 3090). Job `GaWjy48VRM4VdTeQk5H8gzFhr49qbN9HBmLFkK8GCQ42`. |
| 3 | Read the job from Solana | **Works (pure Python)** | 233-byte account, layout as in the research doc. `ipfsJob` equals the pinned CID in all 9 jobs. `payer` is Nosana's shared credits account `credFgXC…`; `project` is ours, `BgvjVh4d…`. No Node.js needed. |
| 3b | What the account's state means | **Research was incomplete** | The account stays **QUEUED** while a node runs the job; state 1 (RUNNING) was never observed. Running is shown by the separate **run account** (`run` in the post response), which exists only while the job runs. **`timeStart` is the listing time** (it equals the `List` tx block time), not the container start. |
| 3c | COMPLETED means success? | **No** | 3 of 9 jobs ended COMPLETED with a failed result: `{status:"failed", opStates:[], errors:[…403…]}`, `{status:"failed", errors:["Failed to publish the job result: fetch failed"]}`, and once **`status:"success"`** with that same publish error and no opStates. `relay_verify/result.py` now requires status `success`, at least one op, every exit code 0, and no errors (5 tests, including the real failed result). |
| 4 | Result content | **Works** | Result JSON fetched by CID from the gateway and hash-checked locally (CIDv0). stdout and stderr are in `opStates[].logs` with type and timestamp. The `ops[].results` regexes work: each name maps to `[full match, group 1]`. The hello string, the inputs SHA-256 and the line count came back as expected. Largest result: 8.0 KB (vLLM, 41 log lines). |
| 4b | Result size limit | **Not found** | No size failure seen up to 8 KB. Two publish failures ("fetch failed") hit a 143-byte result as well, so they aren't about size. The verifier's CIDv0 check needs results ≤ 256 KiB (one block). |
| 5 | Inputs | **Works (embedded)** | `inputs.jsonl` sent base64 in an env var of the job definition. The container's SHA-256 of the decoded inputs equals the one in `meta.relay.inputs_sha256` (`75bb0a9a…`). The inputs are covered by the job definition's CID. |
| 6 | Model loading and startup | **Works, slow and node-dependent** | `vllm/vllm-openai@sha256:607442e4…` (v0.10.2, **by digest**) with `Qwen/Qwen2.5-7B-Instruct@a09a3545…`. Listing to container start: 8–18 s (v0.10.2 is in the 3090 market's `required_images`, so it is cached on its nodes). `import vllm` ~11 s. Download and load **160 s on one node, 1,233 s on another**. First generation 3.1 s. Output `"ready"`. No cached Qwen resource in the market, and **HF `resources` have no revision field**, so the model is downloaded in the container at the pinned revision. |
| 7 | Node targeting | **Works** | `node` on `POST /jobs/list`: the account's `node` equals the requested node (`3hcXmBj7…`), and the parsed results equal the first run's. The job is created by the **`assign`** instruction, not `list`. That's now decoded in `relay_verify/solana.py`, with a test from the real tx. |
| 7b | History fallback | **Works** | Accounts were still readable after completion. `list`/`assign` (ipfsJob, timeout) and `finish` (ipfsResult) decode from `getSignaturesForAddress` → `getTransaction`. Stopping a queued job delists it and **closes** the account. Stopping a running job (`End`) leaves it COMPLETED **with no ipfsResult**. |
| 7c | Billing for early exit | **Billed for run time, not the reservation** | Reservations are 1 h × market price, then settled by use, in steps of about $0.01. The 9 s hello on the 3090 settled $0.01. Hello attempts 1 and 2 (26 s run, plus a job delisted unpicked) settled $0.020 together. The next five 3090 jobs, including ~30 min of vllm attempt 2, settled $0.129 together. |

## Jobs run

| Job | Market | Node | Outcome |
|---|---|---|---|
| `5UrTDacW…` hello, attempt 1 | 3060 | `A1gY6DoP…` | COMPLETED; result failed: node got 403 fetching the job definition (only our own pin) |
| `Ejfzwf3g…` hello, attempt 2 | 3060 | none | never picked up in 20 min (market saturated: 13 nodes busy, 5 jobs queued); delisted |
| `3ASjE9YF…` hello, attempt 3 | 3090 | `3hcXmBj7…` | COMPLETED; result failed: "Failed to publish the job result: fetch failed" |
| `GaWjy48V…` **hello** | 3090 | `2cwhCqyv…` | **success**, 9 s on chain, $0.01 |
| `959C21mq…` **rerun** (node-targeted) | 3090 | `3hcXmBj7…` (requested) | **success**, same parsed results |
| `5tRB9eAw…` vllm, attempt 1 | 3090 | `6YDndB2a…` | COMPLETED, status "success" but publish error, no opStates |
| `3B4LtaAE…` vllm, attempt 2 | 3090 | `4nao4jxJ…` | running; **stopped by our script** after 30 min (it read QUEUED as "not picked up"; fixed) |
| `EXekZDTv…` vllm, attempt 3 | 3090 | `2cwhCqyv…` | exit 1: `TQDM_DISABLE=1` made vLLM 0.10.2 divide by zero (our change; reverted, `use_tqdm=False` instead). Load 159.9 s |
| `Hs7FRTa6…` **vllm** | 3090 | `3hcXmBj7…` | **success**, load 1,233 s, gen 3.1 s, vLLM 0.10.2 |

Failures caused by Nosana's side: 2 publish errors in 7 jobs that a node picked up. Our own mistakes:
attempts 2 and 3 of vllm.

## Startup time

| Stage | Seconds |
|---|---|
| Post → node picks up (3090, idle nodes) | ~4 (run account exists) |
| Listing → container start (image cached) | 8–18 |
| `import vllm` | 10.8–11.5 |
| Download and load 7B weights (15 GB, bf16) | 160–1,233 |
| First generation | 3.1 |
| **Post → result on chain** | **~3.5 min to ~22 min** |

## Credits spent

| | USD |
|---|---|
| `settledCredits` before Phase 0 | 2.804 |
| `settledCredits` after (2026-11-09 23:18) | 2.963 |
| Still reserved for the last vLLM job (upper bound) | 0.175 |
| **Phase 0 real spend** | **≤ 0.334** |
| `spend.json` (reservations counted, conservative) | 0.818 |
| Cap | 3.00 |

## Changes this forces in SPEC_V2 sections 6–8

1. **Receipt `nosana.time_start` / `time_end` (§6).** The account's `timeStart` is the listing time.
   Keep it, named for what it is (`listed_at`), and add `run_start` / `run_end` from the result's
   `startTime` / `endTime` (ms). The verifier checks `listed_at` against the chain and the run times
   against the result.
2. **Verifier check 1 (§7)** becomes: the account exists and is COMPLETED, **and** the result has
   status `success`, at least one op, exit codes 0, no errors (`relay_verify/result.py`). New tamper
   case: point a receipt at a COMPLETED job whose result failed → check 1 fails.
3. **Verifier history fallback** must accept `assign` as well as `list`.
4. **`nosana/ipfs.py` (§4)**: pin each job definition to Nosana's Pinata account (community key) and
   to ours, both via `pinFileToIPFS`, and require the CID to equal our local CIDv0.
5. **`nosana/jobs.py` (§4)**: "running" = the run account exists; the job account's state alone is
   not enough. Stop semantics: `delisted` (queued, account closed) vs `ended` (running, no result).
   Balance in USD.
6. **Output channel (§5.4)**: stdout via `opStates[].logs`, plus `results` regexes for the Merkle root
   and count. The container can't pin `outputs.jsonl` itself, because the job definition is public
   and can't carry a pinning key. Batches must keep the whole result ≤ 256 KiB. Phase 1 should
   measure the size limit with one synthetic 300-line job before the real batch.
7. **Inputs (§5.5)**: embed `inputs.jsonl` in the job definition, so `inputs_cid` = the job
   definition CID plus the hash in `meta`, while the batch stays small. Larger inputs: pin to Nosana's
   account and fetch by CID in the container with a hash check.
8. **Model (§8 Phase 1)**: image by digest works. The weights can't be revision-pinned through
   Nosana's HF resources, so the batch runner downloads at the pinned revision itself. Budget 3–21
   minutes of startup per job, so **one large batch per job**, not many small ones. An option to test:
   use an HF resource for speed and have the container check each file's SHA-256 against the
   revision's LFS hashes before loading.
9. **Markets**: the 3060 market was saturated. The 3090 market (`CA5pMpqk…`, $0.175/h) picked jobs
   up in seconds and keeps vLLM v0.9.2 and v0.10.2 cached. Use it, or another 24 GB market, for
   Phase 1.
10. **Retries**: 2 publish failures in 7 runs. The runner must treat a failed result as retryable,
    and the ledger must keep the failed job as well as the retried one.
11. **Re-runs (§8 Phase 2, POC_PAYOUTS)**: node targeting works. Pick the second node from recent
    COMPLETED jobs in the market minus the nodes currently RUNNING (indexer), as `rerun` now does.

## Fixtures (`tests/fixtures/phase0/`)

- `whoami.json`, `discover_*.json` (balance, templates, markets, prices, 3090 market, indexer stats),
  `final_balance.json`, `spend.json`.
- Per job, prefixed `hello_attempt1..3_`, `hello_`, `rerun_`, `vllm_attempt1..3_`, `vllm_`:
  `jobdef`, `pin_response` (local CID and both Pinata responses), `post_request`, `post_response`,
  `balance_before` / `after` / `after_30s`, `account_getAccountInfo`, `account_decoded`,
  `result_raw` (the node's result, byte-exact), `stop_response` where stopped, `summary`.
- `history_<job>_signatures.json` and `_decoded.json` for `5UrTDacW` (hello attempt 1), `GaWjy48V`
  (hello) and `959C21mq` (rerun, `assign`).
- `rerun_indexer_market_jobs_{running,completed}.json`.

No key, JWT or email is in any fixture (checked before commit).

## Code changed in Phase 0

- `scripts/phase0.py`: balance in USD; Phase 0 cap $3; pinning to Nosana's and our Pinata accounts
  with a CID check; the result check; running detected from the run account; spend recorded when a
  job is stopped; `history` decodes `assign`; `rerun` skips busy nodes; vLLM logs kept small.
- `relay_verify/result.py` (new) and `relay_verify/solana.py` (`assign`).
- `tests/test_verify_result.py` (5 tests), plus 1 new test in `tests/test_verify_primitives.py`.
  **116 tests pass.**
- `docs/phase0-research.md` corrected (marked *Live:*). `.gitignore`: `.nosana-npm/`.

## Decisions for you before Phase 1

- Confirm pinning to Nosana's Pinata account with the community key is acceptable for the grant
  POC. It's what `@nosana/kit` does by default. The alternative is asking Nosana for a pinning route
  for API-key accounts.
- Confirm the receipt time fields (point 1) and the output channel (point 6).
- Startup of 3–21 minutes per job: fine for the batched payout story. It should be stated in the
  application as measured.
