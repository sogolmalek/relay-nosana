from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Deterministic extractors — no LLM calls
# ---------------------------------------------------------------------------

_STACK_FRAME_RE = re.compile(
    r'^\s+(?:at |File ").+[:\(]\d+[:\)]',
    re.MULTILINE,
)
_ERROR_STRING_RE = re.compile(
    r'(?:Error|Exception|Fatal|Traceback)[^\n]{0,120}',
    re.IGNORECASE,
)
_VERSION_RE = re.compile(
    r'\bv?\d+\.\d+(?:\.\d+)?(?:[-+][a-zA-Z0-9.]+)?\b'
)
_OS_RE = re.compile(
    r'\b(?:Ubuntu|Debian|CentOS|RHEL|Windows|macOS|Darwin|Linux)\s[\d.]+\b',
    re.IGNORECASE,
)
_COMMAND_RE = re.compile(
    r'(?:^|\n)\s*(?:\$|>|#)\s+([^\n]{1,120})',
)
# File paths: anything that looks like path/to/file.ext or relative ./foo/bar
_FILE_PATH_RE = re.compile(
    r'(?:^|[\s`\'"])(?:\./|/)?(?:[a-zA-Z0-9_\-]+/){1,8}[a-zA-Z0-9_\-]+\.[a-zA-Z]{1,6}(?=[\s`\'",]|$)',
    re.MULTILINE,
)
# Module/function/class names: CamelCase or dotted identifiers
_SYMBOL_RE = re.compile(
    r'\b(?:[A-Z][a-zA-Z0-9]{2,}(?:\.[A-Z][a-zA-Z0-9]{2,})+|'
    r'[a-z_][a-z0-9_]{2,}(?:\.[a-z_][a-z0-9_]{2,})+)\b'
)


def extract_signals(text: str) -> list[str]:
    """Extract error messages, stack-trace frames, versions, OS, commands."""
    signals: list[str] = []
    for m in _ERROR_STRING_RE.finditer(text):
        s = m.group(0).strip()
        if s and s not in signals:
            signals.append(s)
    for m in _STACK_FRAME_RE.finditer(text):
        s = m.group(0).strip()
        if s and s not in signals:
            signals.append(s)
    for m in _VERSION_RE.finditer(text):
        s = m.group(0).strip()
        if s and s not in signals:
            signals.append(s)
    for m in _OS_RE.finditer(text):
        s = m.group(0).strip()
        if s and s not in signals:
            signals.append(s)
    for m in _COMMAND_RE.finditer(text):
        s = m.group(1).strip()
        if s and s not in signals:
            signals.append(s)
    return signals[:30]


def extract_code_refs(text: str) -> list[str]:
    """Extract file paths, modules, functions, classes mentioned in text."""
    refs: list[str] = []
    for m in _FILE_PATH_RE.finditer(text):
        s = m.group(0).strip().strip("'\"` ")
        if s and s not in refs:
            refs.append(s)
    for m in _SYMBOL_RE.finditer(text):
        s = m.group(0).strip()
        if s and s not in refs and len(s) > 4:
            refs.append(s)
    return refs[:30]


def extractive_summary(text: str, max_sentences: int = 5) -> str:
    """
    Simple extractive summary: first paragraph + key sentences containing
    signals. No LLM; purely deterministic.
    """
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    if not paragraphs:
        return ""

    first_para = paragraphs[0]
    # Split into sentences roughly
    sentences = re.split(r'(?<=[.!?])\s+', text)
    key = []
    for s in sentences[1:]:
        if any(kw in s for kw in ["Error", "error", "fail", "crash", "except", "bug", "fix"]):
            key.append(s.strip())
        if len(key) >= max_sentences - 1:
            break

    parts = [first_para] + key[:max_sentences - 1]
    return " ".join(parts)[:800]
