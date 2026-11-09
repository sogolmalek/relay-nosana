# Relay × Nosana: SPEC v2, the verifiable lane

Read this whole file before writing code. Then read `SPEC.md` (v1) and run `git log --oneline`.
v1 built the measurement pipeline. **v2 adds the part the grant is about: receipts for AI work run on Nosana.**
Everything in v1 stays. Build in the phase order below. **Stop and report at the end of each phase.**

---

## 0. Working rules

- Work only in `~/relay-nosana`. Never touch `~/relay`.
- Use `.venv/bin/python` (Python 3.12) for everything.
- Commit after every phase with a clear message. Git author: Sogol Malek. Push to `origin main` (github.com/sogolmalek/relay-nosana, public).
- **Never commit secrets.** `NOSANA_API_KEY`, `ANTHROPIC_API_KEY`, `GITHUB_TOKEN` and any IPFS pinning key come from env vars only. Before each commit, scan staged files for keys.
- **Ask before spending Nosana credits** beyond the cap in section 12. Every job is stopped in a `finally` block.
- Read-only on huggingface/transformers. The gate runs only on a test repository we own.
- If anything in this spec turns out to be wrong against the real API, stop and report. Don't work around it silently.

## 1. The product decision (do not change this)

Relay accepts AI work from **any source**: a frontier API, a coding agent (Claude Code, Copilot, Devin), or a team's own agent. The team sets **proof rules**: which kinds of work need a verifiable record in Relay's ledger.

- **Work under a proof rule, of a kind an open model has been measured to do well, runs on an open model on Nosana** as a batch job. Each result gets an **anchored receipt** tied to the Nosana job: the job definition (container, model, inputs) on IPFS, and the job, node and timing on Solana.
- **All other work** (no proof rule, or no open model clears the bar) goes to the frontier model and gets a **declared receipt**: what the provider says ran.
- Both kinds of receipt land in **one ledger**.
- The next system or agent acts only on results whose receipt verifies. If a decision changes, work that depends on it is held.

Why Nosana: a frontier API can't produce an anchored receipt, so work that needs proof has to run where execution leaves a public record. Measurement makes the move safe.

A receipt proves **which job definition ran on which node and when**. It does not prove the node computed honestly. Spot re-runs on a second node cover that. Receipts are labelled "anchored and spot-checked", never "cryptographic proof".

## 2. What already exists (v1, keep and reuse)

- Full ingest of huggingface/transformers (19,741 issues, PR files) in `out/cache/`.
- Ground truth for 3 task types: `label` (4,212), `duplicate` (83, shared rule `duplicate_canonical`), `localize` (src/ truth, base-commit trees via the Trees API in `trees_src/`).
- Leakage guards, a fixed dev split (`out/splits/dev.json`), compiler (raw vs compiled at the same token budget), calibration, routing, ledger, report, mock and local providers. 99 tests.
- A Nosana REST client for deployments (`providers/nosana.py`). v2 uses the **Jobs API** instead, because each job is a single on-chain record (section 4).
- `docs/demo/index.html`: a mockup with simulated data. Phase 2 replaces its data with real runs.

## 3. Scope: POC first, then the grant

| | POC (before or with the application) | Grant milestones |
|---|---|---|
| Goal | One real, verifiable loop on Nosana | Production lane and first users |
| Task type | `label` only | `label`, then `duplicate` and `localize` |
| Size | 150–300 issues, 1 open model | Full backlog, 2+ models |
| Deliverables | Batch runner, receipts, verifier, tamper tests, re-run on a 2nd node, gate on a test repo, real-data demo | Receipt spec v1, hosted API, rules, dependency hold, first teams |

Out of scope everywhere: TEE or zero-knowledge proofs, low-latency interactive serving, a public capability-map dataset, posting to real third-party repos, changes to Nosana's platform. Encryption of private inputs comes after the first pilots.

## 4. Architecture

```
work item (any source)
   │
   ▼
rules.py ── needs proof? ──no──► frontier provider ──► declared receipt ─┐
   │yes                                                                   │
   ▼                                                                      │
route.py ── open model cleared the bar? ──no──► frontier (declared) ──────┤
   │yes                                                                   │
   ▼                                                                      │
batch/builder.py   build inputs.jsonl, pin to IPFS                        │
   ▼                                                                      │
nosana/jobdef.py   job definition: image digest, model@revision,          │
                   inputs CID; pin to IPFS                                │
   ▼                                                                      │
nosana/jobs.py     POST job with credits (ipfsHash, market, timeout,      │
                   optional node) → job address                           │
   ▼                                                                      │
[Nosana node]      batch_runner container: fetch inputs, run vLLM         │
                   offline batch, emit outputs.jsonl + merkle root        │
   ▼                                                                      │
nosana/chain.py    read job account from Solana: ipfsJob, ipfsResult,     │
                   node, market, timeStart, state                         │
   ▼                                                                      │
receipts/issue.py  parse result, rebuild Merkle tree, one anchored        │
                   receipt per item ──────────────────────────────────────┤
                                                                          ▼
                                                                      ledger.py
                                                                          │
                     verify/ (relay-verify CLI, no Relay dependency) ◄────┤
                     gate/ (GitHub Action: apply only if verified) ◄──────┘
```

New code:

```
relay_nosana/
  rules.py                 proof rules from relay.rules.yaml
  intake.py                accept work items from any source (JSONL), tag source/vendor
  batch/builder.py         inputs.jsonl from task instances (compiled tasks)
  nosana/jobdef.py         job definition template + pinning
  nosana/jobs.py           Jobs API client (post, get, stop, extend), credits balance
  nosana/chain.py          read job account from Solana (see Phase 0)
  nosana/ipfs.py           pin and fetch (gateway + pinning service)
  receipts/spec.py         receipt schema v0.1 (pydantic), canonical JSON
  receipts/merkle.py       tree, root, path, verify_path
  receipts/issue.py        build receipts from a finished job
  receipts/declared.py     declared receipts for frontier results
  rerun.py                 spot re-run of a sample on a second node, compare parsed outputs
relay_verify/              separate installable package, `relay-verify` CLI
  __main__.py, checks.py, solana.py, ipfs.py
batch_runner/              container image source
  Dockerfile, run.py
gate/                      GitHub Action (composite), uses relay-verify
docs/receipt-spec.md       receipt spec v0.1, human-readable
```

## 5. Phase 0: day-one checks (spend at most $10 of credits, then STOP)

These decide the design. Do them before writing the rest. Record raw API responses as test fixtures.

1. **Pinning.** Find how to pin a job definition to IPFS with an API-key (credits) account. Check `@nosana/kit` `ipfs.pin`, the Jobs API docs (learn.nosana.com/api/jobs.html) and the Swagger. If Nosana's pinning needs a wallet, use a pinning service (Pinata or similar, key from env). Document the choice.
2. **Post a credit job** with our own pinned definition through the Jobs API (`ipfsHash`, `market`, `timeout`). Use a tiny container that prints a known string. Record the response and the job address.
3. **Read the job from Solana.** Confirm the account exposes `ipfsJob`, `ipfsResult`, `node`, `market`, `timeStart`, `state`. Prefer pure Python (Solana JSON-RPC `getAccountInfo` + decode with the Nosana jobs program IDL). Fall back to a small Node helper using `@nosana/kit` only if decoding in Python is impractical. The verifier must stay installable without Node if at all possible.
4. **Result content.** Fetch `ipfsResult` and confirm the container's stdout is there (`opStates[].logs`). Find the size limit. Decide the output channel:
   - stdout, if 300 outputs fit comfortably; or
   - the container pins `outputs.jsonl` itself and prints only its CID and the Merkle root.
5. **Inputs.** Decide how the container gets `inputs.jsonl`: fetched by CID from an IPFS gateway inside the job, or embedded in the job definition if small enough. The inputs CID must appear in the job definition either way.
6. **Model loading.** Check Nosana's cached model resources for the chosen model and market (the vLLM examples use S3 `resources`). Measure startup time.
7. **Node targeting.** Confirm the Jobs API `node` option works for a re-run on a specific second node.

**Report:** a table of each check (works / doesn't / workaround), the raw fixtures, startup time, credits spent, and any change this forces in sections 6–8. **Stop and wait.**

## 6. Receipt spec v0.1

Hashing: SHA-256. Canonical JSON: RFC 8785 (JCS) for anything hashed.

**inputs.jsonl** (one line per item, pinned to IPFS):
`{"item_id": str, "task": str, "messages": [...], "params": {"temperature": 0, "max_tokens": int, "seed": int}}`

**outputs.jsonl** (written by batch_runner):
`{"item_id": str, "input_sha256": hex, "output": {...parsed...}, "output_raw": str, "output_sha256": hex}`
Last line: `{"merkle_root": hex, "count": int, "model": str, "revision": str, "vllm_version": str}`

`input_sha256` = SHA-256 of the canonical JSON of the input line. `output_sha256` = SHA-256 of the canonical JSON of `output` (parsed), so re-runs compare by meaning, not raw text.

**Merkle tree:** leaf = `sha256(0x00 || item_id_utf8 || input_sha256 || output_sha256)`, node = `sha256(0x01 || left || right)`. Leaves sorted by `item_id`. An odd last node is promoted unchanged. Path entries carry a side bit.

**Receipt** (one per item):

```json
{
  "receipt_version": "0.1",
  "grade": "anchored",
  "receipt_id": "sha256 of the canonical receipt without this field",
  "item_id": "...",
  "source": {"vendor": "relay-batch", "work_type": "label"},
  "rule": {"id": "labels-need-proof", "requires": "anchored"},
  "input_sha256": "...",
  "output": {"label": "docs"},
  "output_sha256": "...",
  "merkle": {"root": "...", "path": [{"side": "L", "hash": "..."}]},
  "nosana": {
    "job": "<Solana job address>",
    "market": "<market address>",
    "node": "<node address>",
    "job_definition_cid": "...",
    "result_cid": "...",
    "inputs_cid": "...",
    "time_start": 0,
    "time_end": 0
  },
  "model": {"image": "vllm/vllm-openai@sha256:...", "weights": "org/model@revision"},
  "relay_checks": ["output_schema", "work_type_cleared_bar"],
  "spot_check": {"rerun_job": null, "agreed": null},
  "cost_usd": 0.0
}
```

**Declared receipt** (frontier results): same envelope, `"grade": "declared"`, a `provider` block (`name`, `model`, `request_id`, `timestamp`) instead of `nosana` and `merkle`.

Write the spec as `docs/receipt-spec.md` with a JSON Schema, and example receipts under `docs/examples/`.

## 7. Verification (relay-verify)

`relay-verify receipt.json [--rpc URL] [--ipfs-gateway URL] [--json]`

A separate pip-installable package with no dependency on `relay_nosana`. Checks, in order:

1. The job account exists on Solana and its state is completed.
2. The job definition fetched from IPFS hashes to `job_definition_cid`, and the job account's `ipfsJob` equals it.
3. The image digest and model revision in the job definition equal `model.image` and `model.weights`.
4. `inputs_cid` is referenced by the job definition, and the item's input in that batch hashes to `input_sha256`.
5. The job account's `ipfsResult` equals `result_cid`. The result's outputs contain this item with `output_sha256`, and the Merkle path from the leaf reaches `merkle.root`, which equals the root in the result.
6. `node` and the start time equal the on-chain record.

Exit code 0 means verified, 1 means a check failed (the output names which), 2 means it couldn't check (network). `--json` prints every check with pass/fail and reason. Declared receipts get a schema check and print "declared: not verifiable against a public record".

**Tamper tests (must pass):**
- Change `output.label`: check 5 fails.
- Change `model.weights`: check 3 fails.
- Point `nosana.job` at another real job: check 2 or 4 fails.
- Change one Merkle path hash: check 5 fails.
- Change `node`: check 6 fails.

## 8. Phases

### Phase 1: receipted batch on Nosana (POC core)
- Build and publish the `batch_runner` image (vLLM offline inference, temperature 0, fixed seed, guided JSON for the output schema). Pin the image by digest.
- Model: one 7–8B instruct model available on the chosen market. Record the exact revision.
- Run `label` on the calibration sample (n=100, raw vs compiled) as Nosana jobs, plus a frontier baseline with declared receipts.
- Run a first labelling batch of 150–300 issues (outside the dev split and the calibration sample).
- Issue receipts for every item. Ledger tables: `jobs`, `receipts`, `rules`.
- **Acceptance:** every receipt passes `relay-verify`. Accuracy (raw, compiled, frontier) with CIs reproduces from the ledger. Credits, startup and run time are recorded per job.

### Phase 2: verifier, re-run, gate, real demo (POC complete)
- Package `relay-verify` and run all tamper tests.
- `rerun.py`: re-run a 10% sample (at least 15 items) on a different node using the Jobs API `node` option. Compare parsed outputs and write `spot_check` into the receipts.
- `gate/`: a composite GitHub Action on our own test repo (for example `sogolmalek/relay-gate-demo`). It reads suggestions with receipts and applies a label only if `relay-verify` passes. Include one tampered suggestion that must be rejected.
- `intake.py`: accept a JSONL of work items from any source. Show the ledger holding both grades: anchored (Nosana) and declared (a frontier result).
- Replace the simulated data in `docs/demo/index.html` with real run data (job addresses link to the Nosana explorer, CIDs link to an IPFS gateway). Keep a clear label for anything still simulated.
- README: how to run the POC end to end and how to verify a published receipt.
- **Acceptance:** a third party can clone the repo, `pip install` the verifier and verify the published receipts. The gate demo rejects the tampered suggestion. Demo page shows real data.

### Phase 3: grant M1 hardening (after acceptance)
- Receipt spec v1 with versioning rules. `duplicate` and `localize` through the same lane.
- Full backlog run with receipts. Second open model.
- Cost per 1,000 tasks on Nosana vs a per-token API for the same model, published either way.

### Phase 4: grant M2–M3, product for teams
- Hosted Relay API: submit work, set rules, get results with receipts; ledger search and export.
- Dependency hold: a decision can be superseded; work registered as depending on it is held and the agent is told why.
- Usage reporting per team: receipts, Nosana jobs, GPU-hours, cost.
- Onboarding scripts for open-source repos and pilot teams.

## 9. Rules config (`relay.rules.yaml`)

```yaml
rules:
  - id: labels-need-proof
    work_type: label
    writes_to: github_issue_labels
    requires: anchored          # anchored | declared | none
    bar: {metric: accuracy, ratio_of_frontier: 0.9, use_ci_lower: true}
    on_no_open_model: frontier_declared   # or: human_review
```

## 10. Ledger additions

- `jobs`: job address, market, node, job_definition_cid, inputs_cid, result_cid, image digest, model revision, time_start, time_end, startup_s, credits_before, credits_after, state.
- `receipts`: receipt_id, item_id, grade, rule_id, job address (nullable), output_sha256, merkle_root, verified_at, spot_check_agreed.
- `rules`: rule snapshots by hash, so every receipt points to the exact rule it ran under.
- `dependencies` (Phase 4): decision receipt_id → dependent work id, status (active, held).

## 11. Tests (must pass before any real run in each phase)

- Merkle: known vectors, odd counts, path verification, and that any changed leaf fails.
- Canonical JSON hashing is stable across key order and whitespace.
- Receipt schema validation for both grades.
- Verifier against recorded fixtures (Solana account, job definition, result) from Phase 0: pass case and every tamper case.
- Batch runner: an outputs.jsonl built locally on the mock provider reproduces the same Merkle root.
- Rules and routing: proof-required work never gets a declared receipt unless `on_no_open_model: frontier_declared` is set, and then the ledger says so.
- Teardown: a job is stopped if anything raises after posting.
- No secrets in the repo (a test that scans tracked files).

## 12. Credit and safety limits

- Phase 0: at most $10. Phase 1: at most $40. Phase 2: at most $30. Ask before going over.
- Check balance before every post, and abort if the run's estimate exceeds what's left.
- `relay nosana cleanup` stops every job this repo started that is still running.
- Never post to a real third-party repository.

## 13. Report at the end of each phase

- What was built (files), test count, commits.
- Real numbers: jobs run, items, receipts issued and verified, accuracy with CIs, startup and run time, credits spent, re-run agreement.
- Anything in this spec that turned out wrong against the real API, and what you changed.
- What's next, and any decision you need from me.
