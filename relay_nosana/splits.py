from __future__ import annotations

import json
import random
from pathlib import Path

DEV_SEED: int = 99
DEV_N: int = 100
_DEFAULT_DEV_PATH = Path("out/splits/dev.json")


def create_dev_split(
    pr_closed_issues: list[dict],
    dev_path: Path = _DEFAULT_DEV_PATH,
    *,
    seed: int = DEV_SEED,
    n: int = DEV_N,
) -> set[int]:
    """
    Draw n PR-closed issue numbers as the dev split, persist to dev_path,
    and return the set of issue numbers.

    The dev split is drawn FIRST, before any calibration sample, and is
    excluded from every calibration draw regardless of calibration size.
    dev_path is the single source of truth; call load_or_create_dev_split
    to avoid re-drawing on repeated runs.
    """
    rng = random.Random(seed)
    # Sort first: the draw must depend only on (seed, pool membership),
    # never on the order issues happened to come back from ingest.
    pool = sorted({i["number"] for i in pr_closed_issues})
    rng.shuffle(pool)
    dev_numbers = set(pool[:n])
    dev_path.parent.mkdir(parents=True, exist_ok=True)
    dev_path.write_text(
        json.dumps({"seed": seed, "n": n, "issue_numbers": sorted(dev_numbers)}, indent=2),
        encoding="utf-8",
    )
    return dev_numbers


def load_dev_numbers(dev_path: Path = _DEFAULT_DEV_PATH) -> set[int]:
    data = json.loads(dev_path.read_text(encoding="utf-8"))
    return set(data["issue_numbers"])


def load_or_create_dev_split(
    pr_closed_issues: list[dict],
    dev_path: Path = _DEFAULT_DEV_PATH,
    *,
    seed: int = DEV_SEED,
    n: int = DEV_N,
) -> set[int]:
    """
    Load dev split from dev_path if it exists (stable across runs),
    otherwise draw it, save it, and return it.
    """
    if dev_path.exists():
        return load_dev_numbers(dev_path)
    return create_dev_split(pr_closed_issues, dev_path, seed=seed, n=n)
