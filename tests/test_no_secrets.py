"""No API keys or tokens in tracked files (SPEC v2 section 11)."""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PATTERNS = {
    "anthropic": re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    "openai": re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    "github": re.compile(r"\b(ghp|gho|ghs|ghu|github_pat)_[A-Za-z0-9_]{20,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "assignment": re.compile(r"(NOSANA_API_KEY|ANTHROPIC_API_KEY|GITHUB_TOKEN|PINATA_JWT)\s*=\s*['\"]?[A-Za-z0-9_\-\.]{16,}"),
}


def tracked_files():
    try:
        out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        files = [ROOT / f for f in out.splitlines()]
    except Exception:
        files = [p for p in ROOT.rglob("*") if p.is_file() and ".git" not in p.parts and ".venv" not in p.parts]
    return [f for f in files if f.is_file() and f.stat().st_size < 5_000_000]


def test_no_secrets_in_tracked_files():
    hits = []
    for f in tracked_files():
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        for name, pat in PATTERNS.items():
            if pat.search(text):
                hits.append(f"{f.relative_to(ROOT)}: {name}")
    assert not hits, "possible secrets:\n" + "\n".join(hits)
