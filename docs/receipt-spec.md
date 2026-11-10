# Relay receipt spec v0.2

> **v0.2 (2026-11-10).** Nosana closes a finished job account about an hour after the job (a `CleanAdmin`
> transaction), so a v0.1 receipt, which the verifier checked against the live account, stopped verifying.
> v0.2 anchors on the job's two **transactions**, which can't be closed: `nosana.post_tx` (the `list` or
> `assign` that created the job) and `nosana.finish_tx` (the node's `finish`). The account is a cross-check
> while it exists. A spot check that disagrees now sends the item to human review and says why. Phase 1's and
> run b's receipts were re-issued as v0.2 and all verify (`docs/phase1b-report.md`).

A receipt records one piece of AI work and what can be checked about it. **Anchored** receipts
point to a Nosana job: the job definition (container image, model, inputs) on IPFS, and the job,
node and timing on Solana. **Declared** receipts record what a provider says ran. Both kinds sit in
one ledger. The verifier is `relay_verify/` (standard library only, no Relay code):
`python -m relay_verify receipt.json` (installed as `relay-verify`).

An anchored receipt proves **which job definition ran on which node, and when**. It doesn't prove
the node computed honestly; spot re-runs on a second node cover that. The label is "anchored and
spot-checked", never "cryptographic proof".

## Hashing

- SHA-256 everywhere. Anything hashed is canonical JSON per **RFC 8785 (JCS)**: keys sorted by UTF-16
  code units, no whitespace, ECMAScript number form, integers within ±2^53.
- `input_sha256` = SHA-256 of the JCS of the item's `inputs.jsonl` line.
- `output_sha256` = SHA-256 of the JCS of the **parsed** output, so re-runs compare meaning, not raw
  text.
- `receipt_id` = SHA-256 of the JCS of the receipt without `receipt_id`.

## Merkle tree (one per batch)

- leaf = `sha256(0x00 || item_id (UTF-8) || input_sha256 (32 bytes) || output_sha256 (32 bytes))`
- node = `sha256(0x01 || left || right)`
- Leaves sorted by `item_id`. An odd last node is promoted unchanged.
- A path entry is `{"side": "L"|"R", "hash": hex}`, the sibling's side, from the leaf up.

## Batch files

**inputs.jsonl** (one line per item):
`{"item_id", "task", "messages": [...], "params": {"temperature": 0, "max_tokens", "seed"}}`.
The inputs ride inside the job definition (`env.RELAY_INPUTS_GZ_B64` = base64 of gzip of the
exact `inputs.jsonl` bytes), so the job definition's CID covers them. `inputs_cid` is the CIDv0 of
the `inputs.jsonl` bytes and is named in the job definition's `meta.relay.inputs_cid`. The file is
also pinned on its own.

**Outputs** are printed by the runner (`batch_runner/run.py`, its code is in the job definition) to
stdout and stored by the node in the result it pins to IPFS (`opStates[].logs`):

- `RELAY_OUT {"item_id", "input_sha256", "output": {...}, "output_raw": str, "output_sha256"}`, one per item
- `RELAY_SUMMARY {"merkle_root", "count", "model", "revision", "vllm_version"}`
- `RELAY_MERKLE_ROOT=<hex>`, which the job definition's `results` regex extracts into the result.

Each result stays under 256 KiB (one IPFS block), so anyone can recompute its CID from the bytes.

## Anchored receipt

```json
{
  "receipt_version": "0.2",
  "grade": "anchored",
  "receipt_id": "<sha256 of the receipt without this field>",
  "item_id": "P-0117",
  "source": {"vendor": "relay-batch", "work_type": "payout_check"},
  "rule": {"id": "payouts-need-proof", "requires": "anchored", "snapshot_sha256": "<sha256 of the rule>"},
  "input_sha256": "<hex>",
  "output": {"decision": "hold", "reason_code": "stale_price"},
  "output_sha256": "<hex>",
  "merkle": {"root": "<hex>", "path": [{"side": "L", "hash": "<hex>"}]},
  "nosana": {
    "job": "<Solana job address>",
    "market": "<market address>",
    "node": "<node address>",
    "job_definition_cid": "Qm...",
    "result_cid": "Qm...",
    "inputs_cid": "Qm...",
    "post_tx": "<signature of the job's list or assign transaction>",
    "finish_tx": "<signature of the job's finish transaction>",
    "listed_at": 1794252052,
    "finished_at": 1794252248,
    "container_start": 1794252057815,
    "container_end": 1794252237383
  },
  "model": {"image": "docker.io/vllm/vllm-openai@sha256:...", "weights": "Qwen/Qwen2.5-7B-Instruct@<commit>"},
  "relay_checks": ["output_schema", "work_type_cleared_bar", "spot_check_agreed"],
  "spot_check": {"rerun_job": "<job address>", "rerun_node": "<node>", "agreed": true, "action": "none"},
  "cost_usd": 0.0012
}
```

What the two transactions carry (read from real mainnet transactions of every job in `runs/`; kept as
`runs/*/nosana/*_{post,finish}_tx.json`):

| Transaction | Instruction data | Accounts used | Signed by |
|---|---|---|---|
| `post_tx` | `list` or `assign` (`assign` when the job targets a node): job definition CID (`ipfsJob`, 32-byte digest) and timeout | [0] job, [1] market; `assign` also [3] node | the job keypair (and the poster) |
| `finish_tx` | `finish`: result CID (`ipfsResult`) | [0] job, [2] market, [8] node | the node, alone |

The finish instruction's data carries only the result CID. The node is the instruction's account [8] and
the transaction's only signer, which is stronger than a field in the data: no one else could have sent it.

Time fields:

| Field | Source | Unit |
|---|---|---|
| `listed_at` | block time of `post_tx` (the **listing** time) | unix seconds |
| `finished_at` | block time of `finish_tx` | unix seconds |
| `container_start`, `container_end` | the result's `opStates[0].startTime` / `endTime` | unix **milliseconds**, as the node reports them |

In v0.1 `listed_at` and `finished_at` were the account's `timeStart` and `timeEnd`. The program's clock can
differ from the block time by a second (run b's batch: `timeStart` 1794300984, block time 1794300985), so v0.2
uses the block times, which stay readable after the account is closed.

`spot_check`:

- not re-run: `{"rerun_job": null, "rerun_node": null, "agreed": null, "action": "none"}`
- re-run agreed (same `output_sha256`): `{"rerun_job", "rerun_node", "agreed": true, "action": "none"}`
- re-run disagreed: `{"rerun_job", "rerun_node", "agreed": false, "action": "human_review",
  "rerun_output_sha256", "reason"}`. The schema rejects a disagreement whose action isn't `human_review`.
  The gate (`relay_nosana/gate.py`) holds the payout, and `runs/<run>/review_queue.json` lists the item
  with its reasons. Scores and the bar are still computed from the batch output.

## Declared receipt

Same envelope with `"grade": "declared"`, no `nosana`, `merkle`, `model` or `spot_check`, and:

```json
"provider": {"name": "claude-code", "model": "<model in the JSON output>", "session_id": "<uuid>", "timestamp": "<ISO 8601>"}
```

`provider` needs `name`, `model`, `timestamp`, and one id: `session_id` for headless Claude Code
(`claude -p --output-format json`, the Phase 1 declared lane) or `request_id` for an API call
(`name` `"anthropic"`, the frontier provider kept behind `--api` for later). A declared receipt says
who claims to have run the item; nothing in it can be checked against a public record.

## Verification (relay-verify)

| Check | Passes when |
|---|---|
| schema | the receipt has the v0.2 shape (`relay_verify/schema.py`) |
| receipt_id | it equals the hash of the receipt |
| 1 | `post_tx` and `finish_tx` exist, succeeded, and are this job's: a `list`/`assign` signed by the job keypair, and a `finish` signed by the node it names; both name the same market (and node, for `assign`). The result named by the finish shows the container succeeded (status `success`, every exit code 0, no errors; Phase 0 showed COMPLETED alone isn't enough). If the job account still exists, it is COMPLETED and agrees with the transactions; if Nosana closed it, the check says so and passes on the transactions |
| 2 | the job definition fetched by CID hashes to `job_definition_cid`, and the post transaction's `ipfsJob` equals it |
| 3 | the job definition's image and `MODEL@REVISION` equal `model.image` and `model.weights` |
| 4 | the job definition names `inputs_cid`, its embedded inputs hash to it, and the item's line hashes to `input_sha256` |
| 5 | the finish transaction's `ipfsResult` equals `result_cid`; the result holds this item with this `output_sha256`; `output` hashes to it; the Merkle path reaches `merkle.root`; that root equals the result's summary root, its regex root, and the root recomputed from all outputs in the result |
| 6 | `node` (the finish signer), `market`, `listed_at`, `finished_at` equal the transactions; `container_start`/`end` equal the result |

Every IPFS fetch is accepted only if the bytes hash back to the CID, so any gateway will do. The exit
code is 0 when everything is verified, 1 when a check failed (the output names it), and 2 when a
check couldn't run (network, or an RPC with no record of the transactions).

**RPC and old receipts.** The RPC is `--rpc URL`, else `$RELAY_VERIFY_RPC`, else the public mainnet RPC.
`getTransaction` only works for slots the RPC still holds. Verifying an old receipt needs an **archival
RPC**, one that serves transaction history back to the receipt's slots. Without one, check 1 reports
"could not check" (exit 2) and names the missing transaction; it never reports a pass. A declared receipt gets the schema and `receipt_id` checks and the line
"declared: not verifiable against a public record".

Tamper cases, tested in `tests/test_receipts.py`:

- changed output → check 5
- changed weights → check 3
- pointed at another real job → 2 or 4
- changed Merkle path hash → 5
- changed node → 6
- changed container time → 6 and `receipt_id`
- a COMPLETED job with a failed result → no receipts are issued, and check 1 fails
- `post_tx` pointed at another job's post → 1 (and 2, 5, 6 can't run)
- a finish not signed by the node it names → 1
- a failed transaction → 1
- the job account closed → still verified, from the transactions
- an open account that disagrees with the transactions → 1
- the RPC has no record of a transaction → could not check, with the archival-RPC hint
- a disagreeing spot check marked `action: none` → schema
