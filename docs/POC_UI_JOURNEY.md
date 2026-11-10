# POC app: a reviewer runs the whole journey themselves

Goal: someone on the Nosana team opens one link, runs a **real** payout batch on Nosana, watches it
on Solana, verifies every receipt **in their own browser** (no trust in Relay), tampers with one and
watches it fail, and sees the gate refuse to pay. Nothing on the page is simulated unless it says so.

Builds on `SPEC_V2.md` (receipts, verifier, phases) and `docs/POC_PAYOUTS.md` (scenario).

## 1. The journey (one page, eight steps, each step a screen state)

| # | Step | What the reviewer does | What is real |
|---|---|---|---|
| 1 | **The run** | Sees tonight's synthetic payout run (30 payouts), the team's decisions in force (price change, seller hold, approval rule) and the proof rule `payouts-need-proof` (> $500 → anchored). | Data is synthetic and labelled so. Rule is the real config. |
| 2 | **Route** | Sees which payouts go to Nosana (anchored) and which to the frontier model (declared), and why. | Real `rules.py` + `route.py` output. |
| 3 | **Run on Nosana** | Clicks **Run live on Nosana** (invite code) or **Replay the last real run**. | Live: a real job posted with our credits. Replay: a real earlier job, clearly labelled with its date. |
| 4 | **Watch** | Timeline: job definition pinned (CID link) → listing tx on Solana (explorer link) → picked up by node X → running → result pinned (CID link) → done. Elapsed time and cost shown. | Read from Solana and IPFS by the browser, polling. |
| 5 | **Receipts** | Table: payout, amount, decision, reason, grade (anchored / declared), status. One click opens a receipt. | Receipts issued from the finished job. |
| 6 | **Verify in your browser** | Clicks **Verify**. Six checks run one by one, each with its evidence (account bytes, CID recomputed, Merkle path drawn). | Browser calls Solana RPC and an IPFS gateway directly. Relay's server is not involved. |
| 7 | **Tamper** | Buttons: change the decision, change the model, swap the job, edit a Merkle hash, change the node. Each re-runs verify and shows which check fails. A declared receipt for a $2,400 payout is refused by the rule. | Same browser verifier. |
| 8 | **Pay** | Clicks **Execute payouts**. The gate pays only verified `approve` receipts on Solana **devnet**; each tx memo holds the `receipt_id`. Held and refused payouts are listed with reasons. Second-node re-run agreement shown (N of N). | Real devnet transactions, explorer links. |

Footer, always visible: **Verify it yourself**: `pip install relay-verify` + `relay-verify <receipt>`,
links to the receipts as JSON, the repo, and the receipt spec. And the honest limit, in one line:
"anchored and spot-checked: proves which job ran where and when, not that the node computed honestly;
second-node re-runs cover that."

## 2. Architecture

```
GitHub Pages (static)                     Relay POC server (small, hosted)
docs/app/index.html  ── fetch ──────────► POST /runs         (invite code, rate limit, budget cap)
  │  verifier.js (browser)                GET  /runs/{id}    (status, job address, receipts)
  │    Solana JSON-RPC  ◄── direct        POST /runs/{id}/pay (gate → devnet transfers)
  │    IPFS gateway     ◄── direct        GET  /runs/latest  (replay)
  └  receipts JSON from /runs/{id} or from docs/app/runs/<id>/ (replay, committed)
```

- **Browser verifier** (`docs/app/verifier.js`): a port of `relay_verify` (base58, CIDv0 with
  WebCrypto SHA-256, job-account decode, Merkle path, the six checks). It must give the same
  verdict as the Python CLI on the same receipt: a shared test runs both on the fixtures.
- **Server** (`relay_nosana/server.py`, FastAPI): holds `NOSANA_API_KEY`, `PINATA_JWT`,
  `ANTHROPIC_API_KEY` and a devnet keypair from env. Invite code required for live runs.
  Hard limits: 1 live run at a time, at most 5 per day, stop the job in `finally`, refuse when the
  remaining POC budget is under the run's estimate. Replay needs no code and costs nothing.
- **Hosting (decided):** static app on GitHub Pages; server on **Fly.io** (Sogol's account, `flyctl`
  logged in on her Mac). One small shared-CPU machine with auto-stop/auto-start, one region.
  Secrets only via `fly secrets set` (read from `.env`, never printed). `fly.toml` and the
  Dockerfile are committed; no secret is.
- If no live run is possible (no budget left, server down), the page falls back to replay and
  says so at the top.

## 3. Design

Reuse the visual language of `docs/demo/index.html` (IBM Plex, the actor row, light and dark).
Every hash, CID, address and tx is a link (Nosana explorer, Solana explorer, IPFS gateway) and is
truncated with a copy button. Green/red only for check results. Works at phone width.

## 4. Acceptance: the journey is tested for real

1. **End-to-end journey test** (`tests/e2e_journey/`, Playwright, run manually, costs credits):
   opens the deployed page, enters the invite code, runs live, waits for completion, verifies every
   anchored receipt in the browser (all six checks green), runs every tamper (the expected check
   fails), executes payouts and asserts the devnet txs exist with the right memos. Saves a video
   and screenshots to `out/e2e/` and a JSON summary.
2. **Replay test** (free, in CI): the same journey against the latest committed run.
3. **Fresh-clone test:** in a new venv, `pip install ./relay_verify` and verify all published
   receipts from the CLI; same verdicts as the browser.
4. **Parity test:** browser verifier (run in Node) and Python CLI agree on every fixture, pass and tamper.
5. A recorded 90-second walkthrough video from step 1 goes in the README.

## 5. Budget

Each live run: one batch job + one re-run job (risk-based sample) on a 24 GB market (~$0.19/h,
min 1 h reservation). Estimate ~$0.50 per live run. POC total stays inside SPEC v2 caps
(Phase 1 $40, Phase 2 $30). Devnet SOL is free.
