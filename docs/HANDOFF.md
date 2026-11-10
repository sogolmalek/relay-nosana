# Handoff: where Phase 0 stands (read before doing anything)

> **2026-11-10: Phase 1 is done and committed.** One real payout run on Nosana with receipts:
> `docs/phase1-report.md` (jobs, timing, credits, accuracy with CIs, receipts, decisions needed) and
> `runs/payouts-2026-11-10/`. All 21 anchored receipts pass `relay-verify`; the open model (Qwen2.5-7B)
> scored 4/30 and does **not** clear the bar, so the payouts over $500 go to human review. Phase 1
> spend $0.378 of $3. **Phase 2 waits for Sogol's decisions at the end of the Phase 1 report.**
> Correction to "Rules" below: the balance is USD, not cents; the v1 provider already read it right.

> **2026-11-09: Phase 0 is done and committed.** Results, the checks table, spend and the changes it forces
> on SPEC_V2 sections 6–8 are in `docs/phase0-report.md`. The notes below are the state *before* the
> live runs. Phase 1 waits for Sogol's decisions at the end of the report.

Read in this order: `SPEC_V2.md`, `docs/POC_PAYOUTS.md` (the demo headline: payouts that only go
out with a verified receipt), `docs/phase0-research.md` (what Nosana's own npm packages say about
the Jobs API, the on-chain job account and IPFS). Then `git status`: the work below is **not
committed yet**.

## Done (uncommitted)

- `SPEC_V2.md`, `docs/POC_PAYOUTS.md`, `docs/phase0-research.md`, this file.
- `relay_verify/` (stdlib only): `b58.py`, `cid.py` (CIDv0 of single-block UnixFS files, matches
  the `hello world` vector), `solana.py` (decode the 233-byte Nosana job account; decode `list` and
  `finish` instruction data; JSON-RPC via urllib).
- `scripts/phase0.py` (stdlib only): `discover`, `whoami`, `hello`, `rerun`, `history`, `vllm`.
  Writes raw responses to `tests/fixtures/phase0/`. Stops unfinished jobs in `finally`. Tracks spend
  in `tests/fixtures/phase0/spend.json` and refuses to post past the $10 Phase 0 cap.
- `tests/test_verify_primitives.py` (10 tests), `tests/test_no_secrets.py` (1 test). Both pass.
  The existing 99 v1 tests were not re-run after these changes: run the full suite first.
- `pyproject.toml`: packages now include `relay_verify*`. `.gitignore`: `.env`, `.env.*`.

## Phase 0 results so far

- `discover` ran. Public market data is in `tests/fixtures/phase0/discover_hostmanagermarkets*.json`.
- **Blocker:** `GET /credits/balance` returned **401 Unauthorized** on both
  `https://client-manager.k8s.prd.nosana.com` and `https://api.nosana.com`. The header format
  (`Authorization: Bearer nos_...`) matches `@nosana/api`. Most likely the key value in the shell.
  Run `scripts/phase0.py whoami` (prints the key's shape, never the key, and calls
  `POST /auth/validate-api-key`). If invalid, ask the user for a new key. Do not work around it.

## Next steps (in order)

1. `.venv/bin/python -m pytest -q` (all tests). Fix anything broken by the `relay_verify` addition.
2. `whoami` until balance is HTTP 200. Then re-run `discover` (templates and remote-resources need auth).
3. `hello --market 7AtiXMSH6R1jjBxrcYjehCkkSF7zvYWte63gwEDBcGHq` (NVIDIA 3060, ~$0.05/h).
   Confirms: Pinata CID == local CIDv0; credit job is a real Solana job account; `ipfsJob` equals
   the pinned CID; result fetched and hash-checked; logs contain the hello string; `results` regex
   extraction works; billing for an early-exit job (min timeout is 3600 s).
4. `rerun --market 7AtiX...` (targets a different node; checks the account's `node` equals it).
5. `history <job>` for both jobs (does the account survive; `list`/`finish` decodable from txs).
6. `vllm --market CA5pMpqkYFKtme7K31pNB1s62X2SdhEv1nN9RdxKCpuQ` (NVIDIA 3090, ~$0.19/h).
   Pin the model revision (`--revision <commit sha>` for Qwen/Qwen2.5-7B-Instruct) and check the
   vLLM image tag exists. Records import/load/gen seconds and vLLM version.
7. Write the Phase 0 report (SPEC_V2 section 5): table of checks (works / doesn't / workaround),
   fixture list, startup time, credits spent, changes forced on sections 6–8. Put it in
   `docs/phase0-report.md`.
8. Scan staged files for secrets, run tests, commit as Sogol Malek with a clear message, push to
   `origin main`. **Then stop and report to the user. Do not start Phase 1.**

## Rules that matter here

- Work only in `~/relay-nosana`. Use `.venv/bin/python`.
- Keys from env only (`NOSANA_API_KEY`, `PINATA_JWT`, later `ANTHROPIC_API_KEY`). Never write a key
  or the user's email into a fixture or a commit.
- Phase 0 cap $10 (the script enforces it). Ask before going over.
- If the live API disagrees with `docs/phase0-research.md`, stop and report; don't silently adapt.
- Known v1 bug to fix in Phase 1: `providers/nosana.py` reads the balance as dollars; it is cents.
