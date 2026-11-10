# Phase 2a report: the POC app in replay mode (2026-11-10)

**App:** https://sogolmalek.github.io/relay-nosana/app/ (GitHub Pages, `main:/docs`).
**Mode:** replay of the two real runs that have receipts: Phase 1 (`payouts-2026-11-10`) and run b
(`payouts-2026-11-10b`). **Nosana spend in Phase 2a: $0.** The payout data is synthetic, and the page says so.
The jobs, transactions, results and receipts are real, and the browser checks them live.

## Summary

| Item | Result |
|---|---|
| Browser verifier for receipt v0.2 | `docs/app/verifier.js`, no dependencies. It checks `post_tx` and `finish_tx` and uses the job account only while it exists, like the CLI |
| Parity with the Python CLI | **Identical results** on 72 real receipts, 16 tampers and one v0.1 receipt: same verdict, same pass/fail per check, same reason text (`tests/test_parity.py`, recorded fixtures, no network) |
| Verified live in a browser | Phase 1 **30/30**, run b **42/42**: all six checks green, read from mainnet through PublicNode and from IPFS through Pinata's gateways |
| Tamper buttons | Change the decision → 5; the same without fixing the id → `receipt_id`; change the model → 3; swap the job → 1; point at another job's post → 1; edit a Merkle hash → 5; change the node → 6. The gate refuses every one. A declared receipt used for a $2,400 payout verifies as declared, and the rule refuses it |
| Review queue | Phase 1: 21 payouts over $500. Run b: 42. **P-0128 comes first**, with both reasons: the re-run disagreed, and the open model missed the bar |
| Held over $500 | "held: the open model missed the bar", with the measured numbers. Phase 1: 4/30 = 0.133, lower bound 0.053. Run b (compiled arm): 23/60 = 0.383, lower bound 0.271. The rule needs ≥ 0.90 |
| Devnet payouts | **4 paid on devnet**, each memo = its `receipt_id`, all checked in the browser. Phase 1: 4 pay, 5 hold, 21 human review. Run b: 0 pay, 42 human review, 18 refused (no receipt) |
| Browser RPC | **PublicNode** (`https://solana-rpc.publicnode.com`): no key, CORS `*`. The public mainnet RPC refuses browsers |
| Replay journey test | Playwright, `tests/e2e_journey/`: **3 passed** (Phase 1, run b, phone width), both locally and on the deployed GitHub Pages app, live against mainnet, devnet and IPFS |
| Not in 2a | Live "Run on Nosana" button (shown, disabled), Fly.io server, fresh-clone test, the 90-second video |

## 1. What was built

| File | What |
|---|---|
| `docs/app/index.html`, `app.css`, `app.js` | The eight-step page from `docs/POC_UI_JOURNEY.md`: the run, route, the Nosana jobs, receipts, verify, tamper, human review queue, pay. It reuses the demo's visual language (IBM Plex, light and dark) and works at phone width |
| `docs/app/verifier.js` | A port of `relay_verify`: base58, JCS, CIDv0 (single- and multi-block), Merkle, result parsing, job account decoding, `list`/`assign`/`finish` transaction decoding, the schema, and checks 1–6. It runs in the browser and in Node ≥ 18 |
| `docs/app/gate.js` | A port of `relay_nosana/gate.py`. With the measured bar, a held payout's reason reads "held: the open model missed the bar (…)" |
| `docs/app/runs/<run>/` | `run.json` (payouts, decisions, rules, jobs with their transactions, measured accuracy), receipts copied byte for byte from `runs/`, `review_queue.json`, `payouts.json`. Built by `scripts/build_app_data.py` |
| `scripts/devnet_payouts.mjs` | The payout executor. For each payout it verifies the receipt (same `verifier.js`, live), asks the gate, and only on "pay" sends one devnet transfer plus a Memo instruction whose text is the `receipt_id`. It is idempotent and writes `payouts.json` |
| `tests/parity/`, `tests/test_parity.py`, `tests/fixtures/parity/cache.json` | Parity test, Python CLI vs `verifier.js` in Node, over recorded RPC and IPFS responses |
| `tests/e2e_journey/` | The Playwright replay journey (`npx playwright test`; with `APP_URL=…` it tests the deployed page) |

`docs/index.html` now redirects to the app; the old animated demo stays at `docs/demo/`.

## 2. The browser RPC: PublicNode

Tested with a browser-style request (an `Origin` header and a CORS preflight), using `getTransaction` on Phase 1's
finish transaction:

| RPC | CORS | `getTransaction` from a browser origin |
|---|---|---|
| `api.mainnet-beta.solana.com` (public) | preflight OK | **403 "Access forbidden"** whenever an `Origin` header is present |
| **`solana-rpc.publicnode.com`** | `*` | **works**, no key |
| `solana.drpc.org` | per origin | "chain is not available on free plan" |
| `api.devnet.solana.com` (devnet) | per origin | works; used for the payout checks |

**Choice: PublicNode** for mainnet reads in the browser (`DEFAULT_RPC` in `verifier.js`). Two details:

- PublicNode returns 403 to Python's default `urllib` user agent. `relay_verify` now sends `User-Agent:
  relay-verify/0.2`, so the CLI works against it too: `--rpc https://solana-rpc.publicnode.com`.
- IPFS gateways: `nosana.mypinata.cloud` and `gateway.pinata.cloud` allow any origin and served every CID.
  `ipfs.io` and `dweb.link` allow any origin too, but were rate-limiting (429). They are fallbacks. Every byte is
  hashed back to its CID, so any gateway is safe to use.

**Archival RPC (decided in Phase 2, together with the browser verifier).** PublicNode served every transaction
today. Receipts that must verify months from now need an RPC with full transaction history *and* CORS. Two
candidates to evaluate:

- **Helius**: keyed RPC with historical `getTransaction`.
- **Triton One**: its "Old Faithful" archive serves the full ledger history.

For both, Phase 2 must check two things: how far back `getTransaction` goes for our slots, and whether a
browser key can be restricted to our origin. Neither was tested here.

## 3. Parity: browser verifier = Python CLI

`tests/test_parity.py` runs `verifier.js` in Node and `relay_verify` in Python on the same receipts. Both read
the same recorded RPC and IPFS responses (`tests/fixtures/parity/cache.json`, 4 transactions, 2 closed job
accounts, 4 IPFS objects). For every case the check list must be **equal**: ids, pass/fail/n.a., and the reason
strings.

| Cases | Result |
|---|---|
| 72 published receipts (Phase 1: 21 anchored + 9 declared; run b: 42) | all verified in both |
| 8 tampers × 2 receipts (Phase 1 P-0101, run b P-0128) | same failing checks in both: decision → 5, output only → `receipt_id` + 5, model → 3, job → 1 + 2, post_tx → 1 + 2, Merkle → 5, node → 6, unknown finish tx → could not check (exit 2) |
| the v0.1 P-0101 from git history | schema fails in both, with the re-issue hint |

The Python suite now has 202 tests, all passing.

## 4. Devnet payouts

`node scripts/devnet_payouts.mjs <run>`:

- **Payer:** `2b6sbT9reoEyvdcr61TkXFXCaXErQLSQPv4jp9P3fYtv`, a devnet-only key in the gitignored `out/devnet/`.
  The RPC faucet is closed, so you funded it via faucet.solana.com (5 devnet SOL).
- **Seller addresses:** public keys derived from the seller id. Nobody holds their private keys.
- **Amount:** 1 USD cent = 1,000 lamports.
- **Each transaction:** `SystemProgram.transfer` plus a Memo v2 instruction, signed by the payer. The memo text
  is the receipt's `receipt_id`.

Phase 1, executed 2026-11-10T19:24:52Z. Every receipt was verified live before the gate decided:

| Payout | Amount | Receipt | Devnet tx |
|---|---|---|---|
| P-0109 | $63 | declared, approve, `fd22e631…` | `2qyQueXp…` |
| P-0113 | $294 | declared, approve, `917680b3…` | `2GLnNY3B…` |
| P-0121 | $162 | declared, approve, `394b3540…` | `5E5YcxHJ…` |
| P-0129 | $126 | declared, approve, `eba263fe…` | `2KVCt6B8…` |

- The other 26 payouts:
  - 5 held: the declared model said hold.
  - 21 sent to human review: over $500, and the open model missed the bar.
- Run b: nothing paid.
  - 42 sent to human review.
  - 18 refused: payouts of $500 or less for which run b issued no receipt.
- From the command line, P-0109's transaction has memo `fd22e6313aff…` (its `receipt_id`) and moved exactly
  6,300,000 lamports to the seller's derived address.
- In the app, **Check the devnet payouts in your browser** re-reads each transaction from devnet. It checks:
  - the memo equals the `receipt_id`;
  - the transfer goes to the seller;
  - the receipt verifies;
  - the receipt says approve.

These are declared receipts: the rule `payouts-default` allows them for $500 or less. No anchored receipt has
been paid, because no open model has cleared the bar.

## 5. Journey test (Playwright, replay)

`cd tests/e2e_journey && npx playwright test`. It is free: it reads mainnet, devnet and IPFS from a real
Chromium, as a reviewer would.

1. **Phase 1:**
   - The replay banner shows, and "Run live" is disabled.
   - Verify all → 30 of 30.
   - P-0101: all 8 checks pass.
   - Each of the 7 tampers fails its expected check, and the gate refuses it.
   - A declared receipt used for a $2,400 payout is refused by the rule.
   - The bar note shows 4/30, and 21 payouts are in the queue.
   - Devnet: 4 of 4 transactions checked in the browser.
2. **Run b:**
   - P-0128 is first in the queue, with "re-run disagreed", "held: the open model missed the bar" and 23/60.
   - 42 payouts are in the queue.
   - Verify all → 42 of 42.
   - Each tamper on P-0128 fails as expected.
3. **Phone width (375 px):** no horizontal scroll.

**Result, local server, live network: 3 passed** (Phase 1 21 s, run b 13 s, phone 7 s). **Against the deployed page** (`APP_URL=https://sogolmalek.github.io/relay-nosana/app/`, build `b6f7106`): **3 passed** (48 s). Screenshots, video and
`summary.json` go to `out/e2e/replay/` (not committed).

The first run failed the phone test, which was right to fail: at 375 px the page was 510 px wide. There were three
causes: the run picker's long option text, grid columns that grew to their content's minimum width, and the
unbreakable model id. All three are fixed in `app.css`, and the page is now exactly 375 px wide. Tables scroll
inside their own box.

**Fix needed on the way:** Playwright 1.49 hung silently under Node 24 while loading an ES-module config (it
never listed the tests). The config and spec are CommonJS now, and it runs.

## 6. Run e and spend

- **Run e was posted, then stopped on your instruction.** Your message "Don't post run e yet" arrived after I
  had posted it under your previous go-ahead.
  - Batch 1 (`6hCKz2Fg…`, node `3hcXmBj7…`) ran for 6 minutes. I interrupted the runner, and it stopped the job
    ("ended"). Batch 2 and the re-run were never posted.
  - Settled cost: **$0.017** (3.519 → 3.536).
  - **Phase 1b total: $0.131 of $2.** Nothing is reserved or running.
  - The attempt is recorded in `runs/payouts-2026-11-10e/nosana/` and `nosana.log`. When run e resumes, the runner
    picks this attempt up from its saved post and posts a fresh one.
- **The frontier column couldn't run.** Claude Code on your login answered "You've hit your weekly limit · resets
  Nov 14". The two error rows are set aside in `runs/payouts-2026-11-10e/frontier_attempt1/`. I didn't switch to
  the API key in `.env`, because that would spend API credits.
- **Phase 2a: $0 on Nosana.** Devnet SOL has no value.

## 7. Next step (not started)

- The live **Run on Nosana** button and the Fly.io server (`relay_nosana/server.py`): invite code, one run at a
  time, budget cap, the devnet keypair held server-side.
- Archival RPC choice (section 2).
- The fresh-clone test (`pip install ./relay_verify` in a new venv) and the 90-second recording.
