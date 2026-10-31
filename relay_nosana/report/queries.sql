-- All report numbers must be derivable from these queries.

-- Capability matrix: mean score + parse fail rate per (task, recipient, mode)
SELECT
  task_type,
  recipient_id,
  mode,
  COUNT(*) AS n,
  AVG(score) AS mean_score,
  SUM(CASE WHEN parse_ok=1 THEN 1 ELSE 0 END)*1.0/COUNT(*) AS parse_ok_rate,
  AVG(tokens_in) AS mean_tokens_in,
  AVG(tokens_out) AS mean_tokens_out,
  AVG(latency_ms) AS mean_latency_ms
FROM calls
WHERE run_id = :run_id AND phase = 'calibrate' AND score IS NOT NULL
GROUP BY task_type, recipient_id, mode
ORDER BY task_type, mode, recipient_id;

-- Compiler lift: raw vs compiled per (task, recipient)
SELECT
  task_type,
  recipient_id,
  AVG(CASE WHEN mode='raw' THEN score END) AS raw_score,
  AVG(CASE WHEN mode='compiled' THEN score END) AS compiled_score,
  AVG(CASE WHEN mode='compiled' THEN score END) - AVG(CASE WHEN mode='raw' THEN score END) AS lift
FROM calls
WHERE run_id = :run_id AND phase = 'calibrate' AND score IS NOT NULL
GROUP BY task_type, recipient_id
ORDER BY task_type, recipient_id;

-- Total cost per run
SELECT
  run_id,
  SUM(cost_usd) AS total_cost_usd,
  COUNT(*) AS n_calls
FROM calls
WHERE run_id = :run_id
GROUP BY run_id;

-- GPU hours and credits per deployment
SELECT
  deployment_id,
  model,
  market,
  gpu_hours,
  credits_before - credits_after AS credits_spent
FROM deployments
ORDER BY t_requested DESC;

-- Cost per 1,000 instances
SELECT
  run_id,
  phase,
  task_type,
  mode,
  SUM(cost_usd) * 1000.0 / COUNT(*) AS cost_per_1k_usd
FROM calls
WHERE run_id = :run_id AND cost_usd IS NOT NULL
GROUP BY run_id, phase, task_type, mode;

-- Contamination check: score split by pre/post training cutoff
-- (requires createdAt stored in meta or a separate table)
SELECT
  task_type,
  mode,
  AVG(score) AS mean_score
FROM calls
WHERE run_id = :run_id AND score IS NOT NULL
GROUP BY task_type, mode;
