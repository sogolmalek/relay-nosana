# Relay × Nosana POC — Build Spec

You are building a proof-of-concept for **Relay** in a **new, separate repo: `relay-nosana`** (public, open-source).
The main `relay` repo is under active development by another agent. **Do not write to it.**
Read this whole file before writing code. Build milestone by milestone (section 14).
Stop after **M0** and report the numbers before you continue.

### Relationship to the main `relay` repo
- `relay-nosana` must run on its own from a clean checkout. It has no runtime dependency on `relay`.
- The compiler is behind one interface: `Compiler.compile(instance, recipient_profile) -> TaskPacket`.
  - `DeterministicCompiler` (built here, open-source, the default) implements section 7.
  - `RelayCompilerAdapter` (optional, off by default) loads the main repo's compiler from a path set in `RELAY_CORE_PATH`, **pinned to a commit hash recorded in every run's ledger**. If the path is unset, the adapter is unavailable and nothing else breaks.
- Never copy code from the main `relay` repo into this one.
- Calibration can run both compilers as separate measured conditions (`mode=compiled:deterministic`, `mode=compiled:relay`).

---

## 1. What this POC proves

Relay prepares the next step of a project for the recipient that will do it, based on that recipient's **measured** capability.
This POC applies that to one recipient class: open models running on **Nosana GPUs**.

For a real GitHub repo backlog, the POC must produce, from measurements and not from claims:

1. **Capability:** for each task type, how accurate each open model on Nosana is, compared with a frontier model, measured against the maintainers' own ground truth.
2. **Compiler lift:** how much Relay's compiled task packet improves an open model's accuracy over raw input at the **same token budget**.
3. **Routing:** what share of the work can safely run on Nosana after calibration. Report it twice, with raw input and with compiled packets.
4. **Economics:** cost per 1,000 issues for Relay routing vs frontier-only, plus the GPU-hours used on Nosana.

The headline sentence the report must be able to fill in:
> "Relay's compiler raised the share of work routable to Nosana from **X%** to **Y%**, at **$A** vs **$B** per 1,000 issues, using **H** GPU-hours on Nosana."

If the lift turns out small or negative, report that honestly. Do not tune the eval to manufacture lift.

## 2. Hard constraints

- **Zero work for Nosana's team.** Use only Nosana's public REST API with an API key. No SDK forks, no PRs to Nosana, no custom infrastructure on their side.
- **Real Nosana usage.** Calibration and full runs send real inference requests to vLLM servers deployed on Nosana.
- **Frontier models only for planning, baseline/ceiling measurement, the frontier lane of routing, and final synthesis.** The compiler must **not** call a frontier model per issue (that would defeat the economics). Its per-issue steps are deterministic, plus an optional open-model step that runs on Nosana.
- **Read-only on GitHub.** Never post labels, comments or anything else to the target repo.
- **Honest eval.** No ground-truth leakage into inputs (section 6.3). Same token budget for raw and compiled. Fixed seeds. Confidence intervals on every metric.
- **Credit safety.** Every deployment is stopped in a `finally` block. There is a cleanup command, and a per-run credit cap that aborts the run.

## 3. Stack

- This is a new repo. Use **Python 3.11+** unless `RELAY_CORE_PATH` points to a repo in another language and the adapter can't reasonably be written as a subprocess/CLI call. In that case, call the main repo's compiler through its CLI and keep this repo in Python.
- Libraries: `httpx` (async), `pydantic`, `typer` (CLI), `sqlite3`, `rank_bm25`, `tiktoken` or a model tokenizer for budgets, `jinja2` (report), `pytest`.
- The OpenAI-compatible client works for both vLLM-on-Nosana and local servers. Use the Anthropic SDK for the frontier model.
- Secrets come only from env vars: `NOSANA_API_KEY`, `GITHUB_TOKEN`, `ANTHROPIC_API_KEY`. Never log them.

## 4. Repo layout

```
relay-nosana/
  relay_nosana/
    config.py            # pydantic settings, loads relay.nosana.yaml
    github/
      ingest.py          # GraphQL/REST fetch + on-disk cache
      repo_check.py      # suitability score for a candidate repo
    tasks/
      base.py            # TaskType interface
      label.py
      duplicate.py
      localize.py
      ground_truth.py    # extraction + leakage guards
    compiler/
      packet.py          # TaskPacket model
      compile.py         # raw vs compiled input builders, budget fitting
      extractors.py      # stack traces, error strings, paths, symbols, versions
      retrieval.py       # BM25 over issues and files
    providers/
      base.py            # Recipient interface: complete(prompt, schema) -> result
      nosana.py          # deployment lifecycle + OpenAI client
      local.py           # any OpenAI-compatible URL (ollama/vllm locally)
      mock.py            # deterministic fake for tests
      frontier.py        # Anthropic
    calibrate.py
    route.py
    run.py
    ledger.py            # SQLite ledger
    report/
      build.py
      templates/report.html.j2
    cli.py
  jobs/
    vllm.json.j2         # reusable Nosana job-definition template (public deliverable)
  relay.nosana.yaml      # config
  tests/
  out/                   # run outputs (gitignored)
```

## 5. Data ingest (GitHub)

`relay repo check OWNER/REPO` prints a suitability score. A good target has:
- ≥ 2,000 issues total
- ≥ 30% of closed issues carrying at least one label that maps into the label taxonomy
- ≥ 200 issues closed by a **merged PR** (localization ground truth)
- ≥ 50 issues marked as duplicates
- a meaningful number of issues created after the candidate models' training cutoffs (contamination check)

`relay ingest OWNER/REPO` uses the GitHub GraphQL API to fetch issues with: title, body, author, createdAt, closedAt, state, labels, and comments with timestamps. It also fetches `timelineItems` of types `ClosedEvent` (with `closer` → PullRequest: number, merged, mergeCommit, baseRefOid, changed files), `MarkedAsDuplicateEvent` and `CrossReferencedEvent`.
- Fetch the file tree at the PR's **base commit** (Git Trees API, recursive) for localization tasks.
- Cache everything under `out/cache/OWNER__REPO/`. Make it resumable, respect rate limits (back off on `403`/`429` and secondary rate limits).

## 6. Task types and ground truth

Implement exactly three task types for the POC. Each one implements:

```python
class TaskType(Protocol):
    name: str
    def build_instances(self, repo_data) -> list[TaskInstance]   # only instances with ground truth
    def output_schema(self) -> dict                               # JSON schema for model output
    def score(self, prediction, truth) -> float                   # 0..1
    metric_name: str
```

### 6.1 `label` — issue classification
- Taxonomy: map raw repo labels into a small set via config (e.g. `bug`, `feature`, `docs`, `question`, `other`).
- Instance: a closed issue with exactly one mapped label. Truth = that label.
- Output: `{"label": <enum>}`. Metric: accuracy. Also report macro-F1.

### 6.2 `duplicate` — duplicate detection
- Positive instance: an issue with a `MarkedAsDuplicateEvent`, or closed with a duplicate label plus a "Duplicate of #N" reference. Truth = canonical issue N.
- Negatives: an equal number of non-duplicate issues. Truth = `none`.
- Candidates: the top-k (default 5) older issues by BM25 over title+body. Make sure the true canonical is in the candidate set for positives (log the BM25 recall separately and report it).
- Output: `{"duplicate_of": <issue number or "none">}`. Metric: accuracy.

### 6.3 `localize` — which files need to change
- Instance: an issue closed by a merged PR. Truth = the set of non-test, non-doc source files the PR changed (configurable filters).
- Candidates: the top-k (default 30) files from the base-commit tree by BM25 over path plus extracted identifiers. Log candidate recall separately.
- Output: `{"files": [up to 5 paths]}`. Metric: hit@5 (at least one true file present). Also report recall@5.
- This is the task that ties to "continuing a half-finished project", so treat it as the most important.

### 6.4 Leakage guards (mandatory, unit-tested)
Inputs must reflect only what was knowable **before** resolution:
- Strip all labels from inputs.
- Include only comments with timestamp < the resolution event (label time, duplicate mark, or PR close).
- Remove any text that references the answer: "duplicate of #N", links to the closing PR, `Fixes #`, PR titles, and file paths taken from the PR diff if they appear only in post-resolution comments.
- Tests must assert that the truth label string, the canonical issue number (for positives), and the closing PR number never appear in the built input.

## 7. Compiler: raw vs compiled input

Both builders take `(instance, recipient_profile)` and must fit the **same** token budget, `recipient_profile.input_budget` (derived from the model's context length minus prompt and output reserve).

**Raw (`mode=raw`):** issue title + body + pre-resolution comments, in order, truncated from the end to the budget. Task candidates are appended as a plain list.

**Compiled (`mode=compiled`)** builds a `TaskPacket`:
```
TaskPacket:
  task: one-line instruction for this task type
  summary: 2–4 sentence neutral summary of the issue
  signals: error messages, stack-trace frames, versions, OS, commands run
  code_refs: file paths, modules, functions, classes mentioned
  related: top related issues (number + one-line summary), excluding leaked answers
  candidates: task candidates, each with a short annotation (for localize: path + matching identifiers)
  constraints: output schema reminder
```
- `signals` and `code_refs` come from deterministic extractors (regex plus heuristics). No LLM.
- `summary` uses one of two strategies, set in config: `extractive` (default, no LLM: first paragraph + key sentences) or `open_model` (a short summarization call to the smallest Nosana model; its cost is recorded in the ledger as compiler cost).
- Budget fitting: fill fields in priority order (task, constraints, candidates, signals, code_refs, summary, related) and trim the lowest-priority fields first.
- Serialize the packet as compact, clearly sectioned text. Keep it tokenizer-friendly.

The main repo's Relay compiler plugs in only through `RelayCompilerAdapter` (see the top of this file). The deterministic compiler is always the default and the fallback.

## 8. Providers

### 8.1 Recipient interface
```python
class Recipient(Protocol):
    id: str                 # e.g. "nosana:qwen2.5-14b-instruct-awq@<market>"
    profile: RecipientProfile   # context_len, input_budget, price model
    async def complete(self, prompt: str, schema: dict) -> Completion
        # Completion: text, parsed (dict|None), tokens_in, tokens_out, latency_ms, error
```
Use temperature 0 and a fixed seed. Request JSON output. On vLLM, use structured output (`response_format` with a JSON schema, or guided decoding, whichever the deployed vLLM version supports; detect at startup). If parsing fails, retry once with a repair prompt; if it fails again, the answer scores 0 and the failure is logged. Parse failures are part of capability.

### 8.2 Nosana provider (REST, API key)
Base URL: `https://dashboard.k8s.prd.nos.ci/api`, header `Authorization: Bearer $NOSANA_API_KEY`.
**Before writing this module, fetch the Swagger/API reference linked from Nosana's docs and confirm the endpoint paths and field names below. Write a contract test from a recorded real response.**

Lifecycle per model:
1. `GET /credits/balance`. Available = `assignedCredits − reservedCredits − settledCredits`. Abort if below the run's estimated cost or `max_credits_per_run`.
2. `POST /deployments/create` with `name` (prefix `relay-poc-`), `market`, `timeout` (minutes), `replicas: 1`, `strategy: "SIMPLE"`, `job_definition` rendered from `jobs/vllm.json.j2`.
3. `POST /deployments/{id}/start`.
4. Poll `GET /deployments/{id}` until `endpoints[0].url` exists **and** `GET {url}/v1/models` returns 200. Poll timeout: 30 min (startup can be slow). Record `t_requested`, `t_ready`.
5. Run the batch against `{url}/v1` with an async semaphore (default concurrency 16, configurable), exponential backoff on 5xx and timeouts.
6. Stop the deployment in `finally`, then archive it. Record `t_stopped`.
7. GPU-hours = (`t_stopped − t_requested`) × replicas. Cost = actual credits delta from the balance before and after if available, otherwise GPU-hours × market price from config.

`jobs/vllm.json.j2`: a container job using the `vllm/vllm-openai` image, `gpu: true`, `expose: 8000`, with `cmd` args for `--model`, `--served-model-name`, `--max-model-len`, `--port 8000`, and quantization if needed. Support Nosana's cached model `resources` (S3 URLs) when the chosen market caches the model, which avoids slow downloads. This template is a public deliverable: document it in `jobs/README.md`.

Commands: `relay nosana balance`, `relay nosana up MODEL`, `relay nosana down ID`, `relay nosana cleanup` (stop every running deployment whose name starts with `relay-poc-`), `relay nosana markets` (list markets/GPUs if the API exposes them; otherwise read them from config).

### 8.3 Local and mock providers
- `local`: any OpenAI-compatible base URL (for example Ollama or vLLM on a laptop). Used for development before credits arrive.
- `mock`: deterministic outputs (configurable accuracy per task type) so the whole pipeline and report can be tested end to end with no network.

### 8.4 Frontier provider
Anthropic API; model name from config. It is used for: (a) baseline/ceiling on calibration samples, (b) the frontier lane in the full run, (c) the final report synthesis. It receives the **raw** input (the best-case conventional setup), plus optionally compiled input as an extra measured condition. Record token costs from the configured prices.

## 9. Calibration

`relay calibrate`:
- For each task type, draw a stratified sample (default `n=100`, seed fixed). This sample is held out from routing decisions on the rest of the backlog.
- Run the matrix: {each Nosana model} × {raw, compiled} + frontier(raw). One deployment per model: run **all** task types and both modes while it is up, then tear it down.
- For each cell, compute the metric with a 95% bootstrap CI (1,000 resamples), parse-failure rate, mean tokens in/out, p50/p95 latency, and cost per instance.
- Persist everything in the ledger and `out/<run_id>/calibration.json`.

## 10. Routing

`relay route` produces `routing_table.json`, one row per (task type, mode):
- `eligible(model) := lower_CI(model) ≥ threshold_ratio × frontier_score` (default `threshold_ratio = 0.9`; an absolute floor is optional).
- Choose the **cheapest eligible** Nosana model. If none is eligible, the task type routes to frontier.
- Output the chosen recipient, expected accuracy, expected cost per instance, and the reason.
- Compute the **routable share** = share of backlog instances whose task type routes to Nosana, under raw and under compiled. This is the X% → Y% number.

## 11. Full run

`relay run --mode compiled` (and optionally `--mode raw` for comparison):
- Apply the routing table to **all** instances outside the calibration sample. Nosana-routed work runs on Nosana in batch; the rest runs on frontier.
- Score every instance that has ground truth.
- Also run the routed pipeline on **currently open issues** (no ground truth) and write `out/<run_id>/suggestions.jsonl` (suggested label, possible duplicate, likely files). This is the practical output a maintainer could use. Never post it to GitHub.
- Compare against a frontier-only counterfactual cost, computed from the measured tokens per instance × frontier price. Do not run the full backlog on frontier just to price it.

## 12. Ledger

SQLite at `out/ledger.sqlite`. Table `calls`: `run_id, phase(calibrate|run|compile|synthesis), task_type, instance_id, recipient_id, mode, tokens_in, tokens_out, latency_ms, cost_usd, score, parse_ok, error, ts`.
Table `deployments`: `deployment_id, model, market, t_requested, t_ready, t_stopped, gpu_hours, credits_before, credits_after`.
Every number in the report must be derivable from these two tables with SQL. Ship the queries in `report/queries.sql`.

## 13. Report and public dataset

`relay report RUN_ID` writes:
- `report.md` and a self-contained `report.html`, with:
  - the headline sentence (section 1) filled in
  - the four demo numbers: agreement with maintainer labels; % of work routed to Nosana after calibration; cost per 1,000 issues (Relay vs frontier-only); GPU-hours on Nosana
  - a capability matrix (task × model × mode) with CIs
  - a compiler-lift chart (raw vs compiled per model and task)
  - the routing table with reasons
  - a contamination check: metrics split by issues created before vs after each model's training cutoff
  - limitations
  - a 1-paragraph executive summary written by the frontier model **from the computed numbers only** (pass it the JSON, not raw data)
- `capability_map.csv`, the public "Nosana capability map" dataset, one row per cell: `repo, task_type, model, quantization, market, gpu, mode, n, metric_name, score, ci_low, ci_high, parse_fail_rate, tokens_in_mean, tokens_out_mean, latency_p50_ms, latency_p95_ms, cost_per_1k_usd, gpu_hours, vllm_version, run_id, date`.
- `routing_table.json`, `calibration.json`, `suggestions.jsonl`.

`relay demo OWNER/REPO` runs ingest → build-tasks → calibrate → route → run → report end to end.

## 14. Milestones

**M0: smoke and first real number (do this first, then stop and report).**
- All modules exist. The mock end-to-end test passes.
- One real Nosana deployment: create, start, ready, then a batch of 50 `label` instances in both raw and compiled modes, then stop. Recorded in the ledger.
- Output: accuracy for raw vs compiled for one model, GPU-hours, credits spent, startup time.

**M1: calibration.**
- All three task types, n=100 each, ≥ 2 Nosana models (one small ~7–8B, one mid ~14–32B quantized), both modes, plus the frontier baseline.
- Routing table and routable share (raw vs compiled).

**M2: full run and public deliverables.**
- Full backlog run, suggestions for open issues, complete report, `capability_map.csv`, a documented `jobs/vllm.json.j2`, and a README with exact reproduction steps.

## 15. Config (`relay.nosana.yaml`)

```yaml
repo: OWNER/REPO
seed: 42
label_map: { "type: bug": bug, "kind/bug": bug, "enhancement": feature, "documentation": docs, "question": question }
localize: { exclude_globs: ["**/test*/**", "**/*_test.*", "docs/**", "*.md"], candidates_k: 30 }
duplicate: { candidates_k: 5 }
calibration: { n_per_task: 100, bootstrap: 1000 }
routing: { threshold_ratio: 0.9, abs_floor: null }
compiler: { summary: extractive }   # extractive | open_model
nosana:
  max_credits_per_run: 50
  concurrency: 16
  ready_timeout_min: 30
  deployment_timeout_min: 120
  models:
    - { name: llama-3.1-8b-instruct, hf_id: "<verify>", market: "<verify>", max_model_len: 8192, quant: null, price_per_hour_usd: "<from market>" }
    - { name: qwen2.5-14b-instruct-awq, hf_id: "<verify>", market: "<verify>", max_model_len: 8192, quant: awq, price_per_hour_usd: "<from market>" }
frontier: { model: "<set>", price_in_per_mtok: "<set>", price_out_per_mtok: "<set>" }
```
Fill in every `<verify>`/`<set>` value from Nosana's current market list and the provider's current pricing before running M0. Do not invent them.

## 16. Tests (must pass before any real run)

- Leakage guards (section 6.4) for all three task types.
- Ground-truth extraction on recorded GitHub fixtures.
- Budget fitting: raw and compiled inputs never exceed `input_budget` and are within 10% of each other in token count when the source content is long enough.
- Routing logic on synthetic calibration results, including the case where no model is eligible.
- Ledger: report numbers reproduced from SQL.
- Nosana contract test against recorded responses; a teardown-on-exception test (a deployment is always stopped).
- End-to-end `relay demo` on the mock provider.

## 17. Non-goals

No payments or x402, no UI beyond the static report, no fine-tuning, no writing to GitHub, no changes requested from Nosana, no agent that edits code. Localization predicts files; it does not write patches.

## 18. Definition of done for the POC

- `relay demo OWNER/REPO` reproduces the full report from a clean checkout with three env vars set.
- `relay nosana cleanup` leaves zero `relay-poc-` deployments running.
- The report headline and the four numbers are filled from real Nosana runs, with CIs.
- `capability_map.csv` and `jobs/vllm.json.j2` are documented and ready to publish.
