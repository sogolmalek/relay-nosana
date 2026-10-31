from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any

import httpx

GITHUB_GRAPHQL = "https://api.github.com/graphql"
GITHUB_REST = "https://api.github.com"

_REPO_STATS_QUERY = """
query($owner: String!, $repo: String!) {
  repository(owner: $owner, name: $repo) {
    issues(first: 1) { totalCount }
  }
}
"""

_DUP_LABELS = {"duplicate", "duplicates", "dup"}
_DUP_OF_RE = re.compile(r"duplicate\s+of\s+#\d+", re.IGNORECASE)

# Known bot actor login patterns
_BOT_SUFFIXES = ("[bot]", "-bot", "_bot")
_KNOWN_BOT_LOGINS = {
    "github-actions", "stale", "probot-stale", "dependabot",
    "codecov", "netlify", "vercel", "snyk-bot", "renovate",
    "allcontributors", "imgbot", "lgtm-com", "sonarcloud",
}


def _is_bot_actor(actor: dict | None) -> bool:
    if not actor:
        return False
    typename = actor.get("__typename", "")
    if typename == "Bot":
        return True
    login = actor.get("login", "").lower()
    if any(login.endswith(s) for s in _BOT_SUFFIXES):
        return True
    if login in _KNOWN_BOT_LOGINS:
        return True
    return False


@dataclass
class RepoScore:
    owner: str
    repo: str
    total_issues: int
    labeled_closed_pct: float
    bot_label_pct: float
    pr_closed_issues: int
    duplicate_issues: int
    post_cutoff_issues: int
    bm25_recall_plain: float | None    # path-only BM25 top-30 recall
    bm25_recall_boosted: float | None  # signal-boosted BM25 top-30 recall
    score: float
    details: dict[str, Any]

    def __str__(self) -> str:
        def _pct(v: float | None) -> str:
            return f"{v:.1%}" if v is not None else "N/A"
        lines = [
            f"{self.owner}/{self.repo}  score={self.score:.0f}/100",
            f"  total_issues={self.total_issues}  (need ≥2000)",
            f"  labeled_closed={self.labeled_closed_pct:.1%}  (need ≥30%)",
            f"  bot_label_pct={self.bot_label_pct:.1%}  (need <30%)",
            f"  pr_closed={self.pr_closed_issues}  (need ≥200)",
            f"  duplicates={self.duplicate_issues}  (need ≥50)",
            f"  post_cutoff={self.post_cutoff_issues}",
            f"  bm25@30_plain={_pct(self.bm25_recall_plain)}  →  boosted={_pct(self.bm25_recall_boosted)}",
        ]
        return "\n".join(lines)


async def _graphql(client: httpx.AsyncClient, token: str, query: str, variables: dict) -> dict:
    for attempt in range(5):
        r = await client.post(
            GITHUB_GRAPHQL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables},
            timeout=30,
        )
        if r.status_code in (403, 429):
            retry_after = int(r.headers.get("Retry-After", 60))
            await asyncio.sleep(retry_after)
            continue
        r.raise_for_status()
        payload = r.json()
        if "errors" in payload and "data" not in payload:
            # Secondary rate limit or query complexity issue; back off
            await asyncio.sleep(15 * (attempt + 1))
            continue
        return payload
    raise RuntimeError("GitHub request failed after retries")


_ISSUES_SAMPLE_QUERY = """
query($owner: String!, $repo: String!, $cursor: String, $states: [IssueState!]) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, after: $cursor, states: $states,
           orderBy: {field: UPDATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      totalCount
      nodes {
        number
        state
        title
        body
        createdAt
        labels(first: 10) { nodes { name } }
        timelineItems(first: 20, itemTypes: [
          CLOSED_EVENT, MARKED_AS_DUPLICATE_EVENT, LABELED_EVENT
        ]) {
          nodes {
            __typename
            ... on ClosedEvent {
              closer {
                __typename
                ... on PullRequest {
                  number
                  merged
                }
              }
            }
            ... on MarkedAsDuplicateEvent { createdAt }
            ... on LabeledEvent {
              actor { __typename login }
              label { name }
            }
          }
        }
      }
    }
  }
}
"""

_KNOWN_LABEL_PREFIXES = [
    "bug", "type: bug", "kind/bug",
    "enhancement", "feature", "feature request",
    "type: enhancement", "type: feature",
    "documentation", "docs", "type: docs",
    "question", "help wanted",
]


def _has_mapped_label(labels: list[str]) -> bool:
    for lbl in labels:
        l = lbl.lower().strip()
        if any(l == p or l.startswith(p) for p in _KNOWN_LABEL_PREFIXES):
            return True
    return False


def _is_dup(issue: dict) -> bool:
    labels = [n["name"].lower() for n in issue.get("labels", {}).get("nodes", [])]
    if any(l in _DUP_LABELS for l in labels):
        return True
    for item in issue.get("timelineItems", {}).get("nodes", []):
        if item.get("__typename") == "MarkedAsDuplicateEvent":
            return True
    return False


def _get_closing_pr_number(issue: dict) -> int | None:
    """Return the merged-PR number that closed this issue, or None."""
    for item in issue.get("timelineItems", {}).get("nodes", []):
        if item.get("__typename") == "ClosedEvent":
            closer = item.get("closer")
            if closer and closer.get("__typename") == "PullRequest" and closer.get("merged"):
                return closer.get("number")
    return None


def _is_pr_closed(issue: dict) -> bool:
    return _get_closing_pr_number(issue) is not None


async def _fetch_pr_files(
    client: httpx.AsyncClient, owner: str, repo: str, pr_number: int, token: str
) -> list[str]:
    url = f"{GITHUB_REST}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    r = await client.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        params={"per_page": 100},
        timeout=30,
    )
    if r.status_code != 200:
        return []
    return [f["filename"] for f in r.json()]


def _tokenize(text: str) -> list[str]:
    return re.findall(r'[a-zA-Z0-9_]+', text.lower())


def _bm25_top_k(query: str, docs: list[str], k: int) -> list[int]:
    from rank_bm25 import BM25Okapi
    tokenized = [_tokenize(d) for d in docs]
    if not tokenized:
        return []
    bm25 = BM25Okapi(tokenized)
    scores = bm25.get_scores(_tokenize(query))
    indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    return [i for i, _ in indexed[:k]]


async def _fetch_head_tree(
    client: httpx.AsyncClient, owner: str, repo: str, token: str
) -> list[str]:
    url = f"{GITHUB_REST}/repos/{owner}/{repo}/git/trees/HEAD?recursive=1"
    for attempt in range(3):
        r = await client.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if r.status_code in (403, 429):
            await asyncio.sleep(60)
            continue
        if r.status_code == 404:
            return []
        r.raise_for_status()
        data = r.json()
        return [
            item["path"] for item in data.get("tree", [])
            if item.get("type") == "blob"
        ]
    return []


_EXCLUDE_GLOBS = ["**/test*/**", "**/*_test.*", "**/*_test/**", "docs/**", "*.md", "*.rst"]


def _is_source_file(path: str) -> bool:
    import fnmatch
    return not any(fnmatch.fnmatch(path, g) for g in _EXCLUDE_GLOBS)


async def check_repo(
    owner: str,
    repo: str,
    token: str,
    max_pages: int = 20,
    full_scan: bool = False,
) -> RepoScore:
    """
    Fetch up to max_pages × 100 issues (or all if full_scan=True) and compute suitability.

    Extra checks (M0 additions):
      - bot_label_pct: % of LabeledEvent actors that are bots (require <30%)
      - bm25_recall: on up to 50 PR-closed issues, % where top-30 BM25 over HEAD tree
        contains at least one true changed file
    """
    if full_scan:
        max_pages = 999

    async with httpx.AsyncClient() as client:
        # Get total count
        stats = await _graphql(client, token, _REPO_STATS_QUERY, {"owner": owner, "repo": repo})
        total = stats["data"]["repository"]["issues"]["totalCount"]

        # Sample closed issues
        cursor = None
        closed_issues: list[dict] = []
        for _ in range(max_pages):
            data = await _graphql(
                client, token, _ISSUES_SAMPLE_QUERY,
                {"owner": owner, "repo": repo, "cursor": cursor, "states": ["CLOSED"]},
            )
            nodes = data["data"]["repository"]["issues"]["nodes"]
            page_info = data["data"]["repository"]["issues"]["pageInfo"]
            closed_issues.extend(nodes)
            if not page_info["hasNextPage"]:
                break
            cursor = page_info["endCursor"]

        # Fetch HEAD file tree for BM25 recall
        file_tree = await _fetch_head_tree(client, owner, repo, token)

    source_files = [p for p in file_tree if _is_source_file(p)]

    cutoff = "2023-04-01T00:00:00Z"
    post_cutoff = sum(1 for i in closed_issues if i.get("createdAt", "") >= cutoff)

    # Label counts
    labeled = sum(
        1 for i in closed_issues
        if _has_mapped_label([n["name"] for n in i.get("labels", {}).get("nodes", [])])
    )
    labeled_pct = labeled / len(closed_issues) if closed_issues else 0.0

    # Bot-label share: count LabeledEvent actors
    total_label_events = 0
    bot_label_events = 0
    for issue in closed_issues:
        for item in issue.get("timelineItems", {}).get("nodes", []):
            if item.get("__typename") == "LabeledEvent":
                total_label_events += 1
                if _is_bot_actor(item.get("actor")):
                    bot_label_events += 1
    bot_pct = bot_label_events / total_label_events if total_label_events > 0 else 0.0

    pr_closed = sum(1 for i in closed_issues if _is_pr_closed(i))
    dups = sum(1 for i in closed_issues if _is_dup(i))

    # BM25 recall on up to 50 PR-closed issues (file lists fetched via REST)
    pr_closed_sample = [i for i in closed_issues if _is_pr_closed(i)][:50]
    bm25_recall_plain: float | None = None
    bm25_recall_boosted: float | None = None
    if pr_closed_sample and source_files:
        from ..compiler.retrieval import BM25Index, RRFFusedBM25Index
        from ..compiler.extractors import extract_signals, extract_code_refs

        async with httpx.AsyncClient() as client2:
            pr_files_list = await asyncio.gather(*[
                _fetch_pr_files(client2, owner, repo, _get_closing_pr_number(i), token)
                for i in pr_closed_sample
            ])

        plain_index = BM25Index(source_files)

        hits_plain = 0
        hits_boosted = 0
        evaluated = 0
        for issue, pr_files in zip(pr_closed_sample, pr_files_list):
            truth_files = [f for f in pr_files if _is_source_file(f)]
            if not truth_files:
                continue
            evaluated += 1
            query = f"{issue['title']} {issue.get('body','') or ''}"
            raw_text = f"{issue['title']}\n{issue.get('body','') or ''}"

            # Plain BM25: path-only tokens (no signal boost)
            top30_plain = {source_files[i] for i in plain_index.top_k(query, k=30)}
            if any(f in top30_plain for f in truth_files):
                hits_plain += 1

            # RRF-fused BM25
            signals = extract_signals(raw_text)
            code_refs = extract_code_refs(raw_text)
            boosted_index = RRFFusedBM25Index(
                source_files, signals=signals, code_refs=code_refs
            )
            top30_boosted = {source_files[i] for i in boosted_index.top_k(query, k=30)}
            if any(f in top30_boosted for f in truth_files):
                hits_boosted += 1

        bm25_recall_plain = hits_plain / evaluated if evaluated > 0 else None
        bm25_recall_boosted = hits_boosted / evaluated if evaluated > 0 else None

    # Scoring: each criterion contributes points
    # total_issues: 20 pts
    s1 = min(20, 20 * total / 2000)
    # labeled_pct: 20 pts
    s2 = min(20, 20 * labeled_pct / 0.30)
    # bot_label: 10 pts — full score if <30% bot, 0 if ≥60%, linear between
    if bot_pct < 0.30:
        s_bot = 10.0
    elif bot_pct >= 0.60:
        s_bot = 0.0
    else:
        s_bot = 10.0 * (0.60 - bot_pct) / 0.30
    # pr_closed: 20 pts
    s3 = min(20, 20 * pr_closed / 200)
    # duplicates: 15 pts
    s4 = min(15, 15 * dups / 50)
    # post_cutoff: 10 pts
    s5 = min(10, 10 * post_cutoff / 500)
    # bm25_recall: 5 pts — use boosted if available, else plain
    best_recall = bm25_recall_boosted if bm25_recall_boosted is not None else bm25_recall_plain
    s6 = min(5, 5 * (best_recall or 0) / 0.70) if best_recall is not None else 0.0

    composite = s1 + s2 + s_bot + s3 + s4 + s5 + s6

    return RepoScore(
        owner=owner,
        repo=repo,
        total_issues=total,
        labeled_closed_pct=labeled_pct,
        bot_label_pct=bot_pct,
        pr_closed_issues=pr_closed,
        duplicate_issues=dups,
        post_cutoff_issues=post_cutoff,
        bm25_recall_plain=bm25_recall_plain,
        bm25_recall_boosted=bm25_recall_boosted,
        score=composite,
        details={
            "s_total_issues": s1,
            "s_labeled_pct": s2,
            "s_bot_label": s_bot,
            "s_pr_closed": s3,
            "s_duplicates": s4,
            "s_post_cutoff": s5,
            "s_bm25_recall": s6,
            "sample_size": len(closed_issues),
            "total_label_events": total_label_events,
            "bot_label_events": bot_label_events,
        },
    )
