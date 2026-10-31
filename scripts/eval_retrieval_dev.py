#!/usr/bin/env python3
"""
Dev-split retrieval evaluation for huggingface/transformers.

Uses per-instance base-commit trees as the candidate pool — never HEAD,
never derived from PR files. Trees are fetched only for dev-split instances.

Candidates come from: Trees API, src/ subtree at <baseRefOid> (root if no src/)
Truth comes from    : REST PR files API (cached in pr_files.json), restricted
                      to the same scope as the candidate pool

Reports BM25@30 plain vs RRF-fused on 100 dev issues (seed=99).

Usage:
    GH_TOKEN=... .venv/bin/python scripts/eval_retrieval_dev.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from relay_nosana.compiler.retrieval import BM25Index, RRFFusedBM25Index
from relay_nosana.compiler.extractors import extract_signals, extract_code_refs
from relay_nosana.github.ingest import (
    ingest, count_duplicates_full, _get_closing_pr,
    fetch_file_tree, tree_scope, _default_cache_path,
)
from relay_nosana.splits import load_or_create_dev_split

OWNER = "huggingface"
REPO  = "transformers"
DEV_SEED = 99
DEV_N    = 100
K        = 30

_EXCLUDE_GLOBS = ["**/test*/**", "**/*_test.*", "**/*_test/**", "docs/**", "*.md", "*.rst"]


def _is_source(path: str) -> bool:
    import fnmatch
    return not any(fnmatch.fnmatch(path, g) for g in _EXCLUDE_GLOBS)


async def main() -> None:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("Set GH_TOKEN or GITHUB_TOKEN")

    cache_dir = _default_cache_path(OWNER, REPO)

    # ── Issues + PR files from cache (already fully ingested) ──────────────
    print(f"Loading {OWNER}/{REPO} from cache ...", flush=True)
    issues_path = cache_dir / "issues.json"
    pr_files_path = cache_dir / "pr_files.json"
    if not issues_path.exists() or not pr_files_path.exists():
        sys.exit("Run ingest first: GH_TOKEN=... .venv/bin/python scripts/eval_retrieval_dev.py")

    import json
    issues   = json.loads(issues_path.read_text())
    pr_files = json.loads(pr_files_path.read_text())  # {str(pr_num): [paths]}

    print(f"  {len(issues)} issues loaded", flush=True)

    # ── Duplicate count ─────────────────────────────────────────────────────
    dup_stats = count_duplicates_full(issues)
    print(f"\nDuplicate counts (full ingest, {len(issues)} issues):")
    print(f"  total            : {dup_stats['total']}")
    print(f"  by_label         : {dup_stats['by_label']}")
    print(f"  by_event         : {dup_stats['by_event']}")
    print(f"  by_comment_only  : {dup_stats['by_comment_only']}")

    # ── Build PR-closed list with truth files ───────────────────────────────
    pr_closed_issues: list[dict] = []
    pr_closed_map: dict[int, dict] = {}   # issue_number → {truth, base_ref}
    for issue in issues:
        pr = _get_closing_pr(issue)
        if not pr:
            continue
        files = pr_files.get(str(pr.get("number", "")), [])
        truth = [f for f in files if _is_source(f)]
        if truth:
            base_ref = pr.get("baseRefOid", "")
            pr_closed_issues.append(issue)
            pr_closed_map[issue["number"]] = {"truth": truth, "base_ref": base_ref}

    print(f"\nPR-closed issues with source truth files: {len(pr_closed_issues)}")
    print(f"  (total ingest: {len(issues)}, "
          f"total PRs: {len(pr_files)}, "
          f"with source truth: {len(pr_closed_issues)})")

    # ── Dev split ───────────────────────────────────────────────────────────
    dev_path = Path("out/splits/dev.json")
    dev_numbers = load_or_create_dev_split(
        pr_closed_issues, dev_path=dev_path, seed=DEV_SEED, n=DEV_N
    )
    dev_issues = [i for i in pr_closed_issues if i["number"] in dev_numbers]
    print(f"Dev split: {len(dev_issues)} issues (seed={DEV_SEED}, saved to {dev_path})")

    # ── Fetch per-instance base-commit trees ────────────────────────────────
    print(f"\nFetching base-commit trees for {len(dev_issues)} dev issues ...", flush=True)
    tree_sizes: list[int] = []
    missing_tree = 0
    no_truth_in_scope = 0
    no_src_root = 0
    dev_with_trees: list[tuple[dict, list[str], list[str]]] = []  # (issue, truth, candidates)

    for issue in dev_issues:
        info = pr_closed_map[issue["number"]]
        base_ref = info["base_ref"]
        truth = info["truth"]
        if not base_ref:
            print(f"  WARN: issue #{issue['number']} has no baseRefOid — skipping", flush=True)
            missing_tree += 1
            continue
        try:
            tree = await fetch_file_tree(OWNER, REPO, base_ref, token, cache_dir=cache_dir)
        except RuntimeError as e:
            print(f"  ERROR: issue #{issue['number']} tree ({base_ref[:12]}): {e}", flush=True)
            missing_tree += 1
            continue

        # Listing is src/-only unless the commit predates src/ (root listing)
        scope = tree_scope(tree)
        if not scope:
            no_src_root += 1
        truth = [f for f in truth if f.startswith(scope)]
        if not truth:
            no_truth_in_scope += 1
            continue

        candidates = [f for f in tree if _is_source(f)]
        if not candidates:
            print(f"  ERROR: issue #{issue['number']} tree non-empty ({len(tree)} files) "
                  f"but 0 source candidates after filtering — skipping", flush=True)
            missing_tree += 1
            continue

        tree_sizes.append(len(candidates))
        dev_with_trees.append((issue, truth, candidates))

    evaluated = len(dev_with_trees)
    print(f"\nDev issues with valid trees: {evaluated}/{len(dev_issues)} "
          f"({missing_tree} tree errors, {no_truth_in_scope} with no truth file in scope)")
    print(f"  commits with no src/ (root listing used): {no_src_root}")
    if tree_sizes:
        print(f"  tree size — mean: {sum(tree_sizes)/len(tree_sizes):.0f}  "
              f"min: {min(tree_sizes)}  max: {max(tree_sizes)}")

    if not dev_with_trees:
        print("No evaluable instances — aborting.")
        return

    # ── BM25@30 plain vs RRF ────────────────────────────────────────────────
    hits_plain = hits_rrf = 0
    examples: list[dict] = []
    buckets = {"<1k": [0, 0, 0], "1–2k": [0, 0, 0], ">2k": [0, 0, 0]}  # [n, plain, rrf]

    for issue, truth_files, candidates in dev_with_trees:
        query    = f"{issue['title']} {issue.get('body','') or ''}"
        raw_text = f"{issue['title']}\n{issue.get('body','') or ''}"

        plain_idx = BM25Index(candidates)
        ranked_plain = [candidates[i] for i in plain_idx.top_k(query, k=K)]
        top_plain = set(ranked_plain)
        hit_plain = any(f in top_plain for f in truth_files)
        if hit_plain:
            hits_plain += 1

        signals   = extract_signals(raw_text)
        code_refs = extract_code_refs(raw_text)
        rrf_idx   = RRFFusedBM25Index(candidates, signals=signals, code_refs=code_refs)
        top_rrf   = {candidates[i] for i in rrf_idx.top_k(query, k=K)}
        hit_rrf   = any(f in top_rrf for f in truth_files)
        if hit_rrf:
            hits_rrf += 1

        n = len(candidates)
        b = buckets["<1k" if n < 1000 else "1–2k" if n <= 2000 else ">2k"]
        b[0] += 1
        b[1] += hit_plain
        b[2] += hit_rrf

        if len(examples) < 3:
            examples.append({
                "issue": issue["number"],
                "title": issue["title"][:70],
                "n_candidates": len(candidates),
                "truth": truth_files[:3],
                "top5_plain": ranked_plain[:5],  # rank order
                "hit_plain": hit_plain,
                "hit_rrf": hit_rrf,
            })

    print(f"\n=== Dev-split BM25@{K} recall (n={evaluated}) ===")
    print(f"  plain BM25    : {hits_plain/evaluated:.1%}  ({hits_plain}/{evaluated})")
    print(f"  RRF-fused     : {hits_rrf/evaluated:.1%}  ({hits_rrf}/{evaluated})")
    delta = (hits_rrf - hits_plain) / evaluated
    print(f"  Δ (RRF−plain) : {delta:+.1%}")

    print(f"\n  by candidate-pool size   n    plain     RRF")
    for name, (n, hp, hr) in buckets.items():
        if n:
            print(f"    {name:>5} files      {n:4d}   {hp/n:6.1%}  {hr/n:6.1%}")
        else:
            print(f"    {name:>5} files      {n:4d}       —        —")

    # ── 3 example candidate lists ───────────────────────────────────────────
    print("\n── 3 example instances ──")
    for ex in examples:
        print(f"\n  #{ex['issue']} — {ex['title']}")
        print(f"    candidates pool : {ex['n_candidates']} source files")
        print(f"    truth           : {ex['truth']}")
        print(f"    top-5 plain (by rank): {ex['top5_plain']}")
        print(f"    hit_plain={ex['hit_plain']}  hit_rrf={ex['hit_rrf']}")

    # ── Instance counts for calibration ─────────────────────────────────────
    print("\n── Instance counts ──")
    print(f"  total issues ingested     : {len(issues)}")
    print(f"  PR-closed w/ source truth : {len(pr_closed_issues)}")
    print(f"  dev split (frozen)        : {len(dev_numbers)}")
    print(f"  available for calibration : {len(pr_closed_issues) - len(dev_numbers)}")


if __name__ == "__main__":
    asyncio.run(main())
