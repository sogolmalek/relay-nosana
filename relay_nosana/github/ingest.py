from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from typing import Any

import httpx

GITHUB_GRAPHQL = "https://api.github.com/graphql"
GITHUB_REST = "https://api.github.com"

# ---------------------------------------------------------------------------
# Duplicate-detection helpers (all three sources)
# ---------------------------------------------------------------------------

_DUP_LABELS = {"duplicate", "duplicates", "dup"}
_DUP_OF_RE = re.compile(
    r"duplicate\s+of\s+#(\d+)|dup(?:licate)?\s+of\s+#(\d+)",
    re.IGNORECASE,
)
# Only maintainer comments count as dup-of signals; issue bodies never do
# (a reporter guessing "possible duplicate of #N" is not a verdict).
_MAINTAINER_ASSOC = {"OWNER", "MEMBER", "COLLABORATOR"}
_NEGATION_RE = re.compile(r"\b(not|isn'?t|aren'?t|no longer|wasn'?t)\s+(a\s+)?dup(licate)?", re.IGNORECASE)
_HEDGE_RE = re.compile(
    r"\b(might|maybe|possibl[ey]|perhaps|could\s+be|seems?\s+(like\s+)?a\s+dup)", re.IGNORECASE
)


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _precise_dup_sentence(sentence: str, issue_number: int) -> int | None:
    """Canonical #N if the sentence is an unhedged, non-question, non-negated
    "duplicate of #N" pointing at an older issue; else None."""
    m = _DUP_OF_RE.search(sentence)
    if not m:
        return None
    canonical = int(m.group(1) or m.group(2))
    if (
        canonical >= issue_number
        or _NEGATION_RE.search(sentence)
        or _HEDGE_RE.search(sentence)
        or sentence.rstrip().endswith("?")
    ):
        return None
    return canonical


def maintainer_dup_match(issue: dict) -> tuple[int, str, dict] | None:
    """First precise "duplicate of #N" in a maintainer comment:
    (canonical, sentence, comment), or None."""
    for c in issue.get("comments", {}).get("nodes", []):
        if c.get("authorAssociation") not in _MAINTAINER_ASSOC:
            continue
        for sent in _sentences(c.get("body") or ""):
            canonical = _precise_dup_sentence(sent, issue["number"])
            if canonical is not None:
                return canonical, sent, c
    return None


def _has_dup_label(issue: dict) -> bool:
    return any(n["name"].lower() in _DUP_LABELS for n in issue.get("labels", {}).get("nodes", []))


def _dup_event_canonical(issue: dict) -> int | None | bool:
    """Canonical number from a MarkedAsDuplicateEvent; True if the event has
    no canonical; None if there is no event."""
    for item in issue.get("timelineItems", {}).get("nodes", []):
        if item.get("__typename") == "MarkedAsDuplicateEvent":
            number = (item.get("canonical") or {}).get("number")
            return int(number) if number else True
    return None


def duplicate_canonical(issue: dict) -> int | None:
    """Canonical issue for a duplicate: MarkedAsDuplicateEvent first, then a
    precise maintainer comment. None if neither names one."""
    ev = _dup_event_canonical(issue)
    if ev is not None and ev is not True:
        return ev
    m = maintainer_dup_match(issue)
    return m[0] if m else None


def is_duplicate_issue(issue: dict) -> bool:
    """
    True if the issue is a duplicate by any of:
      1. duplicate/dup/duplicates label
      2. MarkedAsDuplicateEvent in timeline
      3. a precise "Duplicate of #N" in a maintainer comment
         (OWNER | MEMBER | COLLABORATOR; older #N; no hedge, negation or question)
    """
    return (
        _has_dup_label(issue)
        or _dup_event_canonical(issue) is not None
        or maintainer_dup_match(issue) is not None
    )


def count_duplicates_full(issues: list[dict]) -> dict[str, int]:
    """Return per-source breakdown of duplicates plus total unique count."""
    by_label = by_event = by_comment_only = total = 0
    for issue in issues:
        has_label = _has_dup_label(issue)
        has_event = _dup_event_canonical(issue) is not None
        has_comment = maintainer_dup_match(issue) is not None
        by_label += has_label
        by_event += has_event
        by_comment_only += has_comment and not has_label and not has_event
        total += has_label or has_event or has_comment
    return {
        "total": total,
        "by_label": by_label,
        "by_event": by_event,
        "by_comment_only": by_comment_only,
    }


# ---------------------------------------------------------------------------
# GraphQL / REST helpers
# ---------------------------------------------------------------------------

_ISSUES_QUERY = """
query($owner: String!, $repo: String!, $cursor: String) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, after: $cursor, orderBy: {field: CREATED_AT, direction: ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number
        title
        body
        state
        createdAt
        closedAt
        author { login }
        labels(first: 20) { nodes { name } }
        comments(first: 50) {
          nodes {
            body
            createdAt
            authorAssociation
            author { login __typename }
          }
        }
        timelineItems(first: 20, itemTypes: [
          CLOSED_EVENT, MARKED_AS_DUPLICATE_EVENT, CROSS_REFERENCED_EVENT
        ]) {
          nodes {
            __typename
            ... on ClosedEvent {
              createdAt
              closer {
                __typename
                ... on PullRequest {
                  number
                  merged
                  mergedAt
                  baseRefOid
                }
              }
            }
            ... on MarkedAsDuplicateEvent {
              createdAt
              canonical { ... on Issue { number } }
            }
            ... on CrossReferencedEvent {
              createdAt
              source {
                __typename
                ... on PullRequest { number merged }
              }
            }
          }
        }
      }
    }
  }
}
"""


def _default_cache_path(owner: str, repo: str) -> Path:
    return Path(f"out/cache/{owner}__{repo}")


def _atomic_write(path: Path, text: str) -> None:
    """Write text to path atomically via a sibling .tmp file (disk-guarded)."""
    _check_free_space(path.parent)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


async def _graphql(
    client: httpx.AsyncClient,
    token: str,
    query: str,
    variables: dict,
) -> tuple[dict, int | None]:
    """Return (payload, rate_limit_remaining)."""
    for attempt in range(5):
        try:
            r = await client.post(
                GITHUB_GRAPHQL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": variables},
                timeout=30,
            )
            rl_remaining: int | None = None
            try:
                rl_remaining = int(r.headers.get("X-RateLimit-Remaining", ""))
            except (ValueError, TypeError):
                pass

            if r.status_code in (403, 429):
                retry_after = int(r.headers.get("Retry-After", 60))
                await asyncio.sleep(retry_after)
                continue
            r.raise_for_status()
            payload = r.json()
            if "errors" in payload and "data" not in payload:
                await asyncio.sleep(15 * (attempt + 1))
                continue
            return payload, rl_remaining
        except httpx.TimeoutException:
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError("GitHub GraphQL failed after retries")


async def _rest_get(
    client: httpx.AsyncClient, url: str, token: str, params: dict | None = None
) -> Any:
    for attempt in range(4):
        try:
            r = await client.get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params=params,
                timeout=30,
            )
            if r.status_code in (403, 429):
                await asyncio.sleep(int(r.headers.get("Retry-After", 60)))
                continue
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException:
            await asyncio.sleep(2 ** attempt)
    return None


# ---------------------------------------------------------------------------
# Issue fetching — incremental, per-page atomic writes
# ---------------------------------------------------------------------------

async def fetch_issues(
    owner: str,
    repo: str,
    token: str,
    cache_dir: Path | None = None,
    verbose: bool = False,
) -> list[dict]:
    """
    Fetch all issues for owner/repo incrementally.

    Progress is saved after every page: issues are atomically written to
    issues.json and the resume cursor to state.json.  If interrupted,
    the next call loads the saved state and fetches only the missing pages.
    """
    cache_dir = cache_dir or _default_cache_path(owner, repo)
    cache_dir.mkdir(parents=True, exist_ok=True)
    issues_path = cache_dir / "issues.json"
    state_path = cache_dir / "state.json"

    # Load any previously saved progress
    issues_list: list[dict] = (
        json.loads(issues_path.read_text(encoding="utf-8")) if issues_path.exists() else []
    )
    state: dict = (
        json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    )

    if state.get("complete"):
        if verbose:
            print(f"  [{owner}/{repo}] already fully ingested ({len(issues_list)} issues)", flush=True)
        return issues_list

    seen: set[int] = {i["number"] for i in issues_list}
    cursor: str | None = state.get("cursor")
    pages_done: int = state.get("pages_done", 0)

    if verbose and pages_done > 0:
        print(
            f"  [{owner}/{repo}] resuming from page {pages_done + 1}, "
            f"{len(issues_list)} issues already cached",
            flush=True,
        )

    async with httpx.AsyncClient() as client:
        while True:
            payload, rl_remaining = await _graphql(
                client, token, _ISSUES_QUERY,
                {"owner": owner, "repo": repo, "cursor": cursor},
            )
            page_data = payload["data"]["repository"]["issues"]
            page_info = page_data["pageInfo"]
            nodes = page_data["nodes"]

            for node in nodes:
                node["repository"] = f"{owner}/{repo}"
                if node["number"] not in seen:
                    issues_list.append(node)
                    seen.add(node["number"])

            pages_done += 1
            cursor = page_info["endCursor"]
            complete = not page_info["hasNextPage"]

            # Atomic write after every page
            _atomic_write(issues_path, json.dumps(issues_list))
            _atomic_write(state_path, json.dumps({
                "cursor": cursor,
                "pages_done": pages_done,
                "complete": complete,
            }))

            if verbose:
                rl_str = f"  rl_remaining={rl_remaining}" if rl_remaining is not None else ""
                print(
                    f"  [{owner}/{repo}] page {pages_done}  issues={len(issues_list)}{rl_str}",
                    flush=True,
                )

            if complete:
                break

    return issues_list


# ---------------------------------------------------------------------------
# PR file fetching — batch REST, per-PR cached immediately
# ---------------------------------------------------------------------------

async def fetch_pr_files_batch(
    owner: str,
    repo: str,
    pr_numbers: list[int],
    token: str,
    cache_dir: Path | None = None,
    verbose: bool = False,
) -> dict[int, list[str]]:
    """
    Fetch changed file paths for each PR via REST.  Results are cached to
    pr_files.json immediately after each fetch and reused on restart.
    Returns {pr_number: [file_paths]}.
    """
    cache_dir = cache_dir or _default_cache_path(owner, repo)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "pr_files.json"

    # JSON keys are strings; convert back to int on load
    cached: dict[str, list[str]] = (
        json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    )
    missing = [n for n in pr_numbers if str(n) not in cached]

    if verbose and missing:
        print(
            f"  [{owner}/{repo}] fetching files for {len(missing)}/{len(pr_numbers)} PRs via REST ...",
            flush=True,
        )

    sem = asyncio.Semaphore(8)
    done = [0]

    async def fetch_one(pr_num: int) -> None:
        async with sem:
            async with httpx.AsyncClient() as cl:
                url = f"{GITHUB_REST}/repos/{owner}/{repo}/pulls/{pr_num}/files"
                data = await _rest_get(cl, url, token, {"per_page": 100})
                files = [f["filename"] for f in data] if data else []
            cached[str(pr_num)] = files
            # Write cache after every PR so progress is not lost on interruption
            _atomic_write(cache_path, json.dumps(cached))
            done[0] += 1
            if verbose and done[0] % 50 == 0:
                print(
                    f"  [{owner}/{repo}] PR files {done[0]}/{len(missing)} done",
                    flush=True,
                )

    await asyncio.gather(*[fetch_one(n) for n in missing])

    if verbose and missing:
        print(f"  [{owner}/{repo}] PR files complete ({len(pr_numbers)} total)", flush=True)

    return {int(k): v for k, v in cached.items()}


# ---------------------------------------------------------------------------
# File tree fetching — GitHub Trees API (root non-recursive → src recursive)
# ---------------------------------------------------------------------------

_MIN_FREE_BYTES = 5 * 1024 ** 3  # 5 GB


def _check_free_space(path: str | Path = "out") -> None:
    """Raise RuntimeError if less than 5 GB free on the filesystem."""
    usage = shutil.disk_usage(str(path))
    if usage.free < _MIN_FREE_BYTES:
        gb_free = usage.free / 1024 ** 3
        raise RuntimeError(
            f"Disk space guard: only {gb_free:.1f} GB free "
            f"(need ≥5 GB). Free space before continuing."
        )


def tree_scope(paths: list[str]) -> str:
    """Path prefix a fetch_file_tree listing covers: "src/", or "" for a root listing."""
    return "src/" if paths and all(p.startswith("src/") for p in paths) else ""


async def _get_tree(
    client: httpx.AsyncClient, url: str, token: str, params: dict | None = None
) -> dict:
    """GET a git tree; retry on rate limits, raise on every other failure."""
    for attempt in range(4):
        try:
            r = await client.get(
                url, headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60
            )
        except httpx.TimeoutException:
            await asyncio.sleep(2 ** attempt)
            continue
        if r.status_code in (403, 429):
            await asyncio.sleep(int(r.headers.get("Retry-After", 60)))
            continue
        if r.status_code != 200:
            raise RuntimeError(f"GET {url} {params or ''} → HTTP {r.status_code}: {r.text[:200]}")
        return r.json()
    raise RuntimeError(f"GET {url} failed after retries")


async def fetch_file_tree(
    owner: str,
    repo: str,
    ref: str,
    token: str,
    cache_dir: Path | None = None,
) -> list[str]:
    """
    Return blob paths of the source subtree at commit `ref` via the Trees API.

      1. GET /git/trees/{ref} non-recursively; find the root "src" entry.
      2. GET /git/trees/{src_sha}?recursive=1; paths are returned as "src/...".
         If the commit has no root "src" (early history), recurse the root tree.
      3. truncated=true or an empty listing raises — never a partial list.

    Each listing is cached to {cache_dir}/trees_src/{ref}.json.
    """
    cache_dir = cache_dir or _default_cache_path(owner, repo)
    trees_dir = cache_dir / "trees_src"
    trees_dir.mkdir(parents=True, exist_ok=True)
    cache_file = trees_dir / f"{ref}.json"

    if cache_file.exists():
        cached = json.loads(cache_file.read_text(encoding="utf-8"))
        if cached:
            return cached
        cache_file.unlink()  # empty cache is never valid

    base_url = f"{GITHUB_REST}/repos/{owner}/{repo}/git/trees"

    async with httpx.AsyncClient() as client:
        root = await _get_tree(client, f"{base_url}/{ref}", token)
        if root.get("truncated"):
            raise RuntimeError(f"fetch_file_tree: root tree truncated for {owner}/{repo}@{ref[:12]}")
        src_sha = next(
            (e["sha"] for e in root.get("tree", []) if e.get("path") == "src" and e.get("type") == "tree"),
            None,
        )
        target = src_sha or ref
        tree = await _get_tree(client, f"{base_url}/{target}", token, params={"recursive": "1"})

    if tree.get("truncated"):
        raise RuntimeError(
            f"fetch_file_tree: truncated=true for {'src' if src_sha else 'root'} "
            f"of {owner}/{repo}@{ref[:12]} — refusing a partial listing"
        )
    prefix = "src/" if src_sha else ""
    paths = [prefix + e["path"] for e in tree.get("tree", []) if e.get("type") == "blob"]
    if not paths:
        raise RuntimeError(f"fetch_file_tree: 0 paths for {owner}/{repo}@{ref[:12]}")

    _atomic_write(cache_file, json.dumps(paths))
    return paths


# ---------------------------------------------------------------------------
# Full ingest entry point
# ---------------------------------------------------------------------------

def _get_closing_pr(issue: dict) -> dict | None:
    for item in issue.get("timelineItems", {}).get("nodes", []):
        if item.get("__typename") == "ClosedEvent":
            closer = item.get("closer")
            if closer and closer.get("__typename") == "PullRequest" and closer.get("merged"):
                return closer
    return None


async def ingest(
    owner: str,
    repo: str,
    token: str,
    cache_dir: Path | None = None,
    verbose: bool = False,
) -> dict:
    """
    Full ingest: issues + PR changed-file lists.
    All writes are incremental and atomic; safe to interrupt and resume.

    File trees are NOT fetched here: call fetch_file_tree per sampled
    instance (dev split + calibration sample) at its PR's baseRefOid.
    """
    cache_dir = cache_dir or _default_cache_path(owner, repo)
    cache_dir.mkdir(parents=True, exist_ok=True)
    _check_free_space(cache_dir)

    issues = await fetch_issues(owner, repo, token, cache_dir=cache_dir, verbose=verbose)

    # Collect PR numbers for PR-closed issues
    pr_numbers = [pr["number"] for pr in map(_get_closing_pr, issues) if pr]

    pr_files_map = await fetch_pr_files_batch(
        owner, repo, pr_numbers, token, cache_dir=cache_dir, verbose=verbose
    )
    # Attach file lists to closer objects in-memory
    for issue in issues:
        pr = _get_closing_pr(issue)
        if pr and pr.get("number") in pr_files_map:
            pr["changedFiles"] = pr_files_map[pr["number"]]

    if verbose:
        dup_stats = count_duplicates_full(issues)
        print(f"  [{owner}/{repo}] duplicates: {dup_stats}", flush=True)

    # No HEAD tree and no "default" key: a HEAD fallback is what silently
    # invalidated the earlier localize numbers.
    return {"issues": issues, "file_trees": {}}
