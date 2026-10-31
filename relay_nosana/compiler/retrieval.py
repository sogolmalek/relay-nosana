from __future__ import annotations

import re
from typing import Sequence

from rank_bm25 import BM25Okapi


# ---------------------------------------------------------------------------
# Tokenisation helpers
# ---------------------------------------------------------------------------

def _tokenize(text: str) -> list[str]:
    return re.findall(r'[a-zA-Z0-9_]+', text.lower())


def _path_tokens(path: str) -> list[str]:
    """
    Extract all identifier tokens from a file path.

    src/auth/oauth_handler.py  →  [src, auth, oauth_handler, oauth, handler, py]
    lib/OAuthClient.ts         →  [lib, oauthclient, oauth, client, ts]
    """
    # Split on path separators and dots
    raw_parts = re.split(r'[/\\.]', path)
    tokens: list[str] = []
    for part in raw_parts:
        if not part:
            continue
        lower = part.lower()
        tokens.append(lower)
        # Split on underscores and hyphens
        sub = re.split(r'[_\-]', lower)
        tokens.extend(s for s in sub if s and s != lower)
        # Split camelCase: OAuthHandler → oauth, handler
        camel = re.findall(r'[a-z]+|[A-Z][a-z]*', part)
        tokens.extend(s.lower() for s in camel if s.lower() != lower)
    return [t for t in tokens if len(t) > 1]


def _mention_bonus(path: str, mention_set: frozenset[str]) -> bool:
    """
    True if the file is explicitly referenced: any meaningful path token
    (length > 3) appears verbatim in the pre-computed mention token set.
    """
    for tok in _path_tokens(path):
        if len(tok) > 3 and tok in mention_set:
            return True
    # Also check the bare filename without extension
    basename = path.rsplit('/', 1)[-1].rsplit('.', 1)[0].lower()
    if len(basename) > 3 and basename in mention_set:
        return True
    return False


def _build_mention_set(texts: Sequence[str]) -> frozenset[str]:
    """
    Collect every lowercase token that appears in stack traces, error
    messages, or code-ref strings extracted from the issue.
    """
    tokens: set[str] = set()
    for t in texts:
        tokens.update(re.findall(r'[a-zA-Z0-9_]+', t.lower()))
    return frozenset(tokens)


# ---------------------------------------------------------------------------
# Plain BM25 (unchanged interface, used by duplicate/label tasks)
# ---------------------------------------------------------------------------

class BM25Index:
    def __init__(self, docs: list[str]):
        self._docs = docs
        tokenized = [_tokenize(d) for d in docs]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

    def top_k(self, query: str, k: int) -> list[int]:
        """Return indices of top-k documents by BM25 score."""
        if not self._bm25 or not self._docs:
            return list(range(min(k, len(self._docs))))
        scores = self._bm25.get_scores(_tokenize(query))
        indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [i for i, _ in indexed[:k]]

    def top_k_docs(self, query: str, k: int) -> list[str]:
        return [self._docs[i] for i in self.top_k(query, k)]


# ---------------------------------------------------------------------------
# RRF-fused BM25 for file localisation
# ---------------------------------------------------------------------------

class RRFFusedBM25Index:
    """
    Reciprocal Rank Fusion of two ranked lists:
      1. Plain path-token BM25 (query vs expanded path tokens)
      2. Mention-signal list (files whose path tokens appear in extracted
         stack traces / error messages / code refs rank first)

    RRF score = 1/(K + rank_bm25) + 1/(K + rank_mention), K=60.
    Mention matches are capped at MAX_MENTION_MATCHES to prevent over-firing
    on issues that reference many tokens (e.g. long transformer issue bodies).
    Non-mentioned files fill the mention list in BM25 order.

    With no signals the two lists are identical → same ordering as plain BM25.
    """
    RRF_K: int = 60
    MAX_MENTION_MATCHES: int = 10

    def __init__(
        self,
        docs: list[str],
        signals: list[str] | None = None,
        code_refs: list[str] | None = None,
    ):
        self._docs = docs
        mention_texts: list[str] = (signals or []) + (code_refs or [])
        self._mention_set = _build_mention_set(mention_texts)

        expanded = [" ".join(_path_tokens(p)) for p in docs]
        tokenized = [_tokenize(e) for e in expanded]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

        # Count meaningful mention hits per file (path tokens >3 chars in mention_set)
        self._mention_hits: list[int] = [
            sum(1 for t in _path_tokens(p) if len(t) > 3 and t in self._mention_set)
            for p in docs
        ]

    def top_k(self, query: str, k: int) -> list[int]:
        if not self._bm25 or not self._docs:
            return list(range(min(k, len(self._docs))))

        n = len(self._docs)
        K = self.RRF_K

        # BM25 ranking (rank 0 = best)
        bm25_scores = list(self._bm25.get_scores(_tokenize(query)))
        bm25_ranked = sorted(range(n), key=lambda i: bm25_scores[i], reverse=True)
        bm25_rank = {idx: r for r, idx in enumerate(bm25_ranked)}

        # Mention ranking: top MAX_MENTION_MATCHES files with hits > 0 rank first,
        # sorted by hit count desc then BM25 rank asc for ties.
        # Remaining files fill the list in BM25 order.
        mentioned = [(i, h) for i, h in enumerate(self._mention_hits) if h > 0]
        mentioned.sort(key=lambda x: (-x[1], bm25_rank[x[0]]))
        top_mentioned = [i for i, _ in mentioned[:self.MAX_MENTION_MATCHES]]
        top_mentioned_set = set(top_mentioned)
        rest = [i for i in bm25_ranked if i not in top_mentioned_set]
        mention_rank = {idx: r for r, idx in enumerate(top_mentioned + rest)}

        # RRF fusion
        rrf = [
            1.0 / (K + bm25_rank[i]) + 1.0 / (K + mention_rank[i])
            for i in range(n)
        ]
        indexed = sorted(range(n), key=lambda i: rrf[i], reverse=True)
        return indexed[:k]

    def top_k_docs(self, query: str, k: int) -> list[str]:
        return [self._docs[i] for i in self.top_k(query, k)]

    @property
    def mention_count(self) -> int:
        """How many candidate files have at least one mention hit."""
        return sum(1 for h in self._mention_hits if h > 0)


# ---------------------------------------------------------------------------
# Legacy additive-bonus index (kept for backwards compat; use RRFFusedBM25Index)
# ---------------------------------------------------------------------------

class SignalBoostedBM25Index:
    """
    BM25 over path tokens (not raw paths), with an additive mention bonus
    for files whose path components appear in extracted stack traces,
    error messages, or code references from the issue.

    Documents: candidate file paths.
    Query: issue title + body (unchanged).
    Boost: MENTION_BONUS added to any file whose tokens overlap with the
           pre-built mention set (signals + code_refs extracted via
           extractors.py).
    """

    MENTION_BONUS: float = 8.0   # tuned so a single strong mention outweighs
                                  # a weak BM25 score but not a strong one

    def __init__(
        self,
        docs: list[str],
        signals: list[str] | None = None,
        code_refs: list[str] | None = None,
    ):
        self._docs = docs
        mention_texts: list[str] = (signals or []) + (code_refs or [])
        self._mention_set = _build_mention_set(mention_texts)

        # Each document is the expanded set of path tokens (joins as a
        # pseudo-sentence so BM25 can match on identifiers, not just paths)
        expanded = [" ".join(_path_tokens(p)) for p in docs]
        tokenized = [_tokenize(e) for e in expanded]
        self._bm25 = BM25Okapi(tokenized) if tokenized else None

        # Pre-compute mention flags (cheap: done once at index build time)
        self._mentioned = [
            _mention_bonus(p, self._mention_set) for p in docs
        ]

    def top_k(self, query: str, k: int) -> list[int]:
        if not self._bm25 or not self._docs:
            return list(range(min(k, len(self._docs))))

        scores = list(self._bm25.get_scores(_tokenize(query)))
        for i, mentioned in enumerate(self._mentioned):
            if mentioned:
                scores[i] += self.MENTION_BONUS

        indexed = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        return [i for i, _ in indexed[:k]]

    def top_k_docs(self, query: str, k: int) -> list[str]:
        return [self._docs[i] for i in self.top_k(query, k)]

    @property
    def mention_count(self) -> int:
        """How many candidate files received the mention bonus."""
        return sum(self._mentioned)
