# POC headline: payouts that only go out with a verified receipt

Addendum to `SPEC_V2.md`. Everything in v2 still applies (receipt spec, verifier, tamper tests,
phases, credit caps). This file changes **which scenario the demo leads with**.

## Why this scenario

- A frontier API can't produce a public record of what ran. A Nosana job can: job definition on
  IPFS, job / node / timing on Solana. Payments are where a team most wants that record.
- Payout runs are **already batched** in the real world (marketplace payouts, accounts payable,
  refund queues). Batching on Nosana adds minutes to a process that takes hours or days. The delay
  is not something we invented to create jobs.
- The `label` lane from SPEC v2 stays, as the scale and accuracy evidence (150–300 issues, accuracy
  vs frontier with CIs). It is the second exhibit, not the headline.

## The story (one payout run)

A marketplace pays its sellers every night. An agent (any vendor) prepares the run: 20–40 payouts.
The team's rule: **payouts over $500 need an anchored receipt.**

1. Relay reads the run and the team's ledger of decisions (example: "the Pro plan dropped from $45
   to $42 on Nov 3"; "seller S-114 is on a fraud hold"; "refunds over 30 days need finance approval").
2. Work under the rule goes to an open model on Nosana as **one batch job**. For each payout the
   model returns `{"decision": "approve" | "hold", "reason_code": str}`.
3. Relay issues one anchored receipt per payout (Merkle path to the batch root, job on Solana).
4. The payout executor (a gate) sends a payout **only if `relay-verify` passes** and the decision
   is `approve`. Payout = a Solana **devnet** transfer whose memo carries the `receipt_id`.
5. Payouts under the threshold take the frontier lane and get a **declared** receipt. The gate
   accepts declared receipts only where the rule allows it.

Chain of evidence the demo shows: **payout tx → receipt → Nosana job → node → job definition
(image, model, inputs)**, every link checkable without trusting Relay.

## Task: `payout_check`

Input (one line of `inputs.jsonl`): the payout (seller, amount, currency, line items, reason), plus
the decisions in force that the compiler selects for it.

Output schema (guided JSON):

```json
{"decision": "approve", "reason_code": "ok"}
{"decision": "hold", "reason_code": "stale_price" | "seller_on_hold" | "needs_approval" | "duplicate" | "amount_mismatch"}
```

Ground truth is computed by deterministic rules over the synthetic ledger, so accuracy is measured,
not judged. The data is **synthetic and labelled as such**: job definitions and inputs are public on
IPFS, so no real customer data goes into a job (encryption is post-pilot, see SPEC v2 section 3).

## Spot re-runs: risk-based

- Every payout above $5,000: always re-run on a second node.
- Everything else: a 10% sample (at least 15 items), as in SPEC v2.
- Re-runs compare parsed outputs (`output_sha256`), not raw text, because vLLM is not bit-exact
  across GPUs.

## Demo, 90 seconds (recorded from a real run, not live)

1. **Run:** the payout run arrives; 23 payouts need proof; one Nosana job; receipts issued.
2. **Verify:** `pip install relay-verify && relay-verify receipts/P-0117.json` → six green checks,
   read straight from Solana and IPFS. Job address links to the Nosana explorer.
3. **Tamper:** flip one `hold` to `approve` in a receipt → check 5 fails, the gate refuses to pay.
   The same decision from a frontier API carries a declared receipt → the gate refuses it for a
   payout over $500. Second-node re-run: N of N agree.
4. **Pay:** devnet payouts land; each memo holds its `receipt_id`.

## What this changes in SPEC v2

| SPEC v2 | Now |
|---|---|
| POC task: `label` only | Headline task: `payout_check`. `label` kept as the scale lane |
| Gate: GitHub Action on a test repo | Gate: payout executor on Solana devnet. The GitHub gate stays optional |
| Re-run: 10% sample | Risk-based: all high-value items + 10% sample |
| Rule example `labels-need-proof` | Adds `payouts-need-proof` (below) |

```yaml
rules:
  - id: payouts-need-proof
    work_type: payout_check
    writes_to: payouts
    when: {amount_usd_gt: 500}
    requires: anchored
    bar: {metric: accuracy, ratio_of_frontier: 0.9, use_ci_lower: true}
    on_no_open_model: human_review      # money: never fall back to declared silently
    rerun: {always_if: {amount_usd_gt: 5000}, sample_rate: 0.10, sample_min: 15}
```

## Honest limits (say them in the application)

- A receipt proves which job definition ran on which node and when. It does not prove the node
  computed honestly; re-runs on a second node cover that. Label: "anchored and spot-checked".
- Startup is minutes, not milliseconds (measured in Phase 0). This lane is for batched work where
  proof matters more than latency. Real-time checks stay on Relay's deterministic check path.
- Devnet payouts are a demonstration of the gate, not a payments product.
