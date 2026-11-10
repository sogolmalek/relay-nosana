# Feedback for Nosana (for the grant application)

Things we hit building receipts on the Jobs API with credits. Each is measured, with the fixture or
report that shows it.

1. **No pinning route for API-key (credits) accounts.** Nodes fetch job definitions only through
   `nosana.mypinata.cloud`, which answers 403 for CIDs that aren't pinned to Nosana's Pinata account.
   A credits account has no pin endpoint, so the only working route is the community Pinata key that
   `@nosana/ipfs` ships as a public default. We use it for the POC (plus a second pin on our own
   account), but a team building on credits should not depend on a shared, rate-limited key. Ask: a
   `POST /ipfs/pin` (or an upload field on `POST /jobs/list`) for API-key accounts, or a node fallback
   to public gateways with CID verification. (Phase 0, `hello_attempt1_result_raw.json`.)
2. **COMPLETED on chain doesn't mean the container ran.** Failed node-side fetches and failed result
   publishes still end COMPLETED with a result CID; once the result even said `status: "success"`
   with an error and no opStates. Ask: a distinct on-chain state or a flag for failed runs, and a
   consistent `status` in the result. (Phase 0 report, check 3c.)
3. **The result publish fails sometimes** ("Failed to publish the job result: fetch failed", 2 of 7
   picked-up jobs in Phase 0). The job is lost and has to be posted again. Ask: node-side retry of
   the publish.
4. **The job account stays QUEUED while running**, and `timeStart` is the listing time. Both are
   reasonable, but undocumented; the docs' RUNNING state is never set for credit jobs. Ask: document
   the run account as the running signal.
5. **HF model resources have no revision field**, so a cached model can't be pinned to a commit.
   Receipts need `org/model@revision`, so we download in the container instead (3 to 21 minutes per
   job, node-dependent). Ask: `revision` on HF resources.
6. **Balance units are undocumented.** `GET /credits/balance` is USD; `creditsUsed` in the post
   response is thousandths of a dollar.
7. **A stopped running job leaves nothing behind**: no result, no logs, and the job account shows no
   node. In Phase 1 two jobs on two 3090 nodes ran for 55 minutes without finishing; we stopped them
   and can't say why they hung (85% of the Phase 1 spend). Ask: publish the logs so far when a job is
   stopped or times out, and keep the node on the job account. (Phase 1 report, jobs table.)
8. **Unhealthy nodes stay in the market**: one 3090 node (`6YDndB2a…`) failed every job we sent it
   because its GPU driver doesn't initialise (`NVMLError_Unknown`), and failed a result publish in
   Phase 0. Ask: a node health check (e.g. `nvidia-smi` before taking a job) that takes failing
   nodes out of the queue.
