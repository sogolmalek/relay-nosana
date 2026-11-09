# Phase 0 research: what the Nosana packages say (before spending credits)

Source: the published npm packages, read directly (no docs site was reachable from the build
environment): `@nosana/api@2.24.4` (OpenAPI types for client-manager, host-manager,
blockchain-indexer), `@nosana/kit@2.24.4`, `@nosana/jobs-program@2.24.3` (Codama-generated account
and instruction codecs), `@nosana/ipfs@2.24.3`, `@nosana/types@2.24.4`, `@nosana/sdk@0.4.87`.
Everything below is **what the code says**; Phase 0 confirms it against the live API.

> **Corrected by the live API on 2026-11-09** (see `docs/phase0-report.md`): the balance is in
> **USD**, not cents; job definitions must be pinned to **Nosana's** Pinata account, not only ours;
> and an on-chain state of COMPLETED does **not** mean the container succeeded. The corrections are
> marked *Live:* below.

## Jobs API (credits, API key)

| | |
|---|---|
| Base URL | `https://client-manager.k8s.prd.nosana.com` (default in `@nosana/api`). The docs' `https://api.nosana.com` is tried too; Phase 0 records which answers. |
| Auth | `Authorization: Bearer <NOSANA_API_KEY>` |
| Post a job | `POST /jobs/list` body `{ipfsHash, market, timeout?, node?}`. Optional header `Idempotency-Key`. |
| Response | `{tx, job, run, credits: {costUSD, creditsUsed, reservationId, project}}`: `job` is the Solana job address, `tx` the listing transaction. |
| Timeout | **Credit jobs must have `timeout >= 3600` s**, else 400. Phase 0 measures whether a job that ends early is billed for the run time or the full reservation. |
| Node targeting | `node` field on `POST /jobs/list` (single post only; the batch endpoint has no `node`). *Live:* works; the account's `node` equals the requested node. The job is created with the `assign` instruction instead of `list`. |
| Stop / extend | `POST /jobs/{address}/stop` → `{tx, job, delisted, outcome}`; `POST /jobs/{address}/extend` body `{seconds}`. |
| Balance | `GET /credits/balance` → `{assignedCredits, reservedCredits, settledCredits}`. *Live:* these are **USD** (`assignedCredits: 10` = $10; a $0.044 job moved `reservedCredits` by 0.044). The cents reading here was wrong, and v1 `providers/nosana.py` reading dollars is **correct**. The post response's `credits.creditsUsed` is in thousandths of a dollar (`creditsUsed: 44` = `costUSD: 0.044`). |
| Pinning | **No pin endpoint for API-key accounts.** The caller pins the job definition. `@nosana/kit` pins through Pinata (`/pinning/pinJSONToIPFS`) with a community key that `@nosana/ipfs` documents as intentionally public. *Live:* a definition pinned only with our own key (`PINATA_JWT`) **fails on the node**: the node fetches via `nosana.mypinata.cloud`, which answers 403 for CIDs not pinned to Nosana's Pinata account. We now pin the same bytes with `pinFileToIPFS` to Nosana's account (community key, read at runtime, never committed) and to ours for durability, and check both CIDs equal our local CIDv0. |
| Markets, cached models | host-manager: `GET /markets/`, `GET /markets/{id}`, `GET /markets/remote-resources`. Indexer: `GET /jobs?market=&state=&node=`, `GET /jobs/{address}`. |

## On-chain job account (pure-Python decodable)

Program `nosJhNRqr2bc9g1nfGDcXXTXvYUmxD4cVwy2pMWhrYM` (mainnet). Account size 233 bytes, Borsh, little-endian:

| offset | field | type |
|---|---|---|
| 0 | discriminator | 8 bytes `[91,16,162,5,45,210,125,65]` |
| 8 | ipfsJob | 32 bytes (sha2-256 digest; CID = base58(`0x12 0x20` + digest), i.e. CIDv0) |
| 40 | ipfsResult | 32 bytes, same encoding |
| 72 | market | pubkey |
| 104 | node | pubkey |
| 136 | payer | pubkey |
| 168 | price | u64 |
| 176 | project | pubkey |
| 208 | state | u8: 0 QUEUED, 1 RUNNING, 2 COMPLETED, 3 STOPPED |
| 209 | timeEnd | i64 |
| 217 | timeStart | i64 |
| 225 | timeout | i64 |

Consequences for the design:

- The verifier needs only Solana JSON-RPC `getAccountInfo` + base58 + this layout. **No Node.js.**
- Because the on-chain value is a CIDv0 (sha256 of a dag-pb/UnixFS node), the verifier can fetch the
  job definition and result bytes from **any** gateway and recompute the CID itself. A lying gateway
  can't pass. Holds for files up to 256 KiB (single block); larger results need chunked-DAG
  hashing, so keep each result under 256 KiB in the POC.
- The program has `close`, `clean` and `cleanAdmin` instructions, so **a finished job account may be
  closed later**. The `list` instruction data carries `ipfsJob` + `timeout`, and `finish` carries
  `ipfsResult`, so the verifier can fall back to the job's transaction history
  (`getSignaturesForAddress` → `getTransaction` → decode). Phase 0 checks whether accounts survive.

Instruction discriminators: `list` `[54,174,193,67,17,41,132,38]` (data: ipfsJob[32], timeout i64),
`finish` `[67,74,170,132,125,233,182,37]` (data: ipfsResult[32]).

*Live (2026-11-09):*

- A job posted **with `node`** is created by **`assign`** `[73,66,125,203,81,41,64,135]`
  (`sha256("global:assign")[:8]`, data ipfsJob[32], timeout i64, same as `list`), not `list`. The
  history fallback must accept both.
- The job account **stays QUEUED (0) while a node runs it**; state 1 was never observed. The running
  state is the separate **run account** (`run` in the `POST /jobs/list` response), which exists only
  while the job runs and is closed afterwards.
- **`timeStart` is the listing time** (it equals the `List` transaction's block time), not the
  container start. The container's own start and end are in the result (`startTime`, `endTime`, ms).
- Stopping a **queued** job: `outcome: "delisted"`, the job account is closed. Stopping a **running**
  job: `outcome: "ended"`, an `End` instruction, the account ends **COMPLETED with no ipfsResult**.
- Finished job accounts were still readable after completion (checked minutes later; whether they are
  closed later is not known).

## Job definition and result

- Job definition: `{version, meta?, global?, ops: [{type: "container/run", id, args: {image, cmd,
  env, gpu, resources, required_vram, ...}, results?: {key: regex}}]}`.
- **`ops[].results`** maps a name to a regex over the container logs; the node puts matches in the
  result. The batch runner prints `RELAY_MERKLE_ROOT=<hex>` and the job definition extracts it, so
  the root is a structured field of the node-pinned result, not only a log line.
- Result (pinned by the **node**, CID written on-chain by `finish`): `{status, startTime, endTime,
  opStates: [{operationId, status, exitCode, logs: [{type, log, timestamp}], results, ...}]}`.
- *Live:* a job whose node could not even fetch the definition still ends **COMPLETED** on-chain,
  with a `finish` transaction and a result of `{status: "failed", opStates: [], errors: [...]}`. The
  verifier must check the result's `status`, every op's `exitCode` and `errors`, not only the
  account state (`relay_verify/result.py`).
- Job definitions are public on IPFS: **no secrets and no private data in a job definition**. Inputs
  for the POC are synthetic.

## Open questions Phase 0 answers live

1. Which base URL works with our key; raw balance response.
2. Pin with `PINATA_JWT`; Pinata's returned CID equals our locally computed CIDv0.
3. A credit job is a real job account on Solana with the fields above; who is `payer`/`project`.
4. Result content: logs present, `results` regex extraction works, size.
5. Billing for a job that exits early (balance before/after).
6. `node` targeting: the account's `node` equals the requested node.
7. Account still readable after completion; `list`/`finish` transactions decodable as fallback.
8. vLLM startup time for one 7–8B model on the chosen market (separate step, after 1–7 pass).
