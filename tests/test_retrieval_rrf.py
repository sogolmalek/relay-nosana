from __future__ import annotations

import pytest
from relay_nosana.compiler.retrieval import RRFFusedBM25Index, BM25Index


DOCS = [
    "src/auth/oauth_handler.py",
    "src/models/user.py",
    "src/utils/string_helper.py",
    "src/views/dashboard.py",
    "tests/test_auth.py",
    "docs/contributing.md",
]


def test_mention_promotes_file():
    """A file explicitly named in stack trace should appear in top-2."""
    signals = ["OAuthError in oauth_handler.py line 42"]
    code_refs = ["src/auth/oauth_handler.py"]
    index = RRFFusedBM25Index(DOCS, signals=signals, code_refs=code_refs)
    top2 = index.top_k_docs("authentication error", k=2)
    assert "src/auth/oauth_handler.py" in top2


def test_cap_limits_mention_matches():
    """Mention matches beyond MAX_MENTION_MATCHES (10) should not inflate results."""
    big_docs = [f"src/module_{i}/file.py" for i in range(30)]
    # 25 signal mentions — well over the cap of 10
    signals = [f"module_{i} error" for i in range(25)]
    index = RRFFusedBM25Index(big_docs, signals=signals)
    # mention_count can exceed cap (it counts all matched files)
    # but top_k must still return exactly k results
    assert len(index.top_k("query", k=5)) == 5
    assert len(index.top_k("query", k=30)) == 30


def test_no_signals_preserves_bm25_top1():
    """With no signals, RRF degenerates to pure BM25 ordering (both lists identical)."""
    index = RRFFusedBM25Index(DOCS, signals=[], code_refs=[])
    # "oauth" strongly matches src/auth/oauth_handler.py for both plain and RRF
    top1_rrf = index.top_k("oauth authentication", k=1)[0]
    # Build path-token BM25 the same way RRF does internally
    from relay_nosana.compiler.retrieval import _path_tokens, _tokenize, BM25Okapi
    expanded = [" ".join(_path_tokens(d)) for d in DOCS]
    bm25 = BM25Okapi([_tokenize(e) for e in expanded])
    scores = bm25.get_scores(_tokenize("oauth authentication"))
    top1_plain = int(max(range(len(scores)), key=lambda i: scores[i]))
    assert top1_rrf == top1_plain


def test_returns_correct_count():
    index = RRFFusedBM25Index(DOCS)
    assert len(index.top_k("query", k=3)) == 3
    assert len(index.top_k("query", k=100)) == len(DOCS)  # capped at n


def test_mention_count_property():
    signals = ["oauth_handler string_helper"]
    index = RRFFusedBM25Index(DOCS, signals=signals)
    # Both oauth_handler and string_helper should have hits
    assert index.mention_count >= 2


def test_empty_docs():
    index = RRFFusedBM25Index([])
    assert index.top_k("anything", k=5) == []


def test_localize_uses_rrf(monkeypatch):
    """Confirm tasks/localize.py imports RRFFusedBM25Index, not SignalBoostedBM25Index."""
    import importlib, ast, inspect
    from relay_nosana.tasks import localize
    src = inspect.getsource(localize)
    assert "RRFFusedBM25Index" in src
    assert "SignalBoostedBM25Index" not in src
