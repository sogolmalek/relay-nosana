// The payout gate, a port of relay_nosana/gate.py: pay only on a receipt that verifies, has the grade the
// rule requires, and leaves no doubt. Everything else is held, sent to human review, or refused, with reasons.

const RANK = { none: 0, declared: 1, anchored: 2 };

// bar: the measured numbers for the open model ({model, correct, n, ci_low, ...}) and the rule's bar, so a
// held payout says exactly why: "held: the open model missed the bar".
export function decide(receipt, verdict, rule, bar = null) {
  if (!receipt) return { action: "refuse", reasons: ["no receipt for this payout"] };
  if (receipt.rule.id !== rule.id) return { action: "refuse", reasons: [`receipt is under rule ${receipt.rule.id}, not ${rule.id}`] };
  if (RANK[receipt.grade] < RANK[rule.requires]) {
    return { action: "refuse", reasons: [`rule ${rule.id} requires an ${rule.requires} receipt; this one is ${receipt.grade}`] };
  }
  if (verdict !== null && verdict !== 0) {
    return { action: "refuse", reasons: [verdict === 1 ? "receipt failed relay-verify" : "relay-verify couldn't check the receipt"] };
  }
  const review = [];
  const sc = receipt.spot_check || {};
  if (sc.action === "human_review") review.push(`re-run disagreed: ${sc.reason}`);
  if (rule.requires === "anchored" && !(receipt.relay_checks || []).includes("work_type_cleared_bar")) {
    review.push(bar ? `held: the open model missed the bar (${bar.measured.model}: ${bar.measured.correct}/${bar.measured.n}`
      + ` = ${bar.measured.accuracy.toFixed(3)}, Wilson 95% lower bound ${bar.measured.ci_low.toFixed(3)};`
      + ` the rule needs ≥ ${bar.needed_ci_low.toFixed(2)})`
      : `the open model has not cleared rule ${rule.id}'s bar (on_no_open_model: ${rule.on_no_open_model || "human_review"})`);
  }
  if (review.length) return { action: "human_review", reasons: review };
  if (receipt.output.decision !== "approve") {
    return { action: "hold", reasons: [`decision ${receipt.output.decision}/${receipt.output.reason_code}`] };
  }
  if (verdict === null) return { action: "pay_after_verify", reasons: ["approve; pays only once relay-verify passes"] };
  return { action: "pay", reasons: [] };
}
