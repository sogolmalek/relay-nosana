from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class RoutingDecision:
    task_type: str
    mode: str
    chosen_recipient_id: str | None
    expected_score: float | None
    ci_lo: float | None
    ci_hi: float | None
    reason: str
    routes_to_nosana: bool


def compute_routing_table(
    calibration_cells: list[dict],
    threshold_ratio: float = 0.9,
    abs_floor: float | None = None,
) -> list[RoutingDecision]:
    """
    For each (task_type, mode):
    - Find frontier score
    - Find cheapest eligible Nosana model (lower_CI ≥ threshold_ratio × frontier_score)
    - If none eligible, route to frontier
    """
    from collections import defaultdict

    # Group by task_type → mode → recipient
    cells_by: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for cell in calibration_cells:
        key = (cell["task_type"], cell["mode"])
        cells_by[key].append(cell)

    decisions: list[RoutingDecision] = []
    all_task_modes = set(cells_by.keys())
    task_types = sorted({k[0] for k in all_task_modes})
    modes = ["raw", "compiled"]

    for task_type in task_types:
        for mode in modes:
            key = (task_type, mode)
            cells = cells_by.get(key, [])
            if not cells:
                continue

            # Find frontier score
            frontier_cells = [c for c in cells if c["recipient_id"].startswith("frontier:")]
            frontier_score = None
            if frontier_cells:
                frontier_score = frontier_cells[0].get("mean_score")

            threshold = (
                (frontier_score or 0.0) * threshold_ratio
                if frontier_score is not None
                else None
            )

            # Nosana candidates
            nosana_cells = [c for c in cells if c["recipient_id"].startswith("nosana:")]

            eligible: list[dict] = []
            for cell in nosana_cells:
                ci_lo = cell.get("ci_lo")
                mean_score = cell.get("mean_score")
                if ci_lo is None or mean_score is None:
                    continue
                passes_threshold = (threshold is None) or (ci_lo >= threshold)
                passes_floor = (abs_floor is None) or (ci_lo >= abs_floor)
                if passes_threshold and passes_floor:
                    eligible.append(cell)

            if eligible:
                # Choose cheapest (by tokens_in_mean as proxy; could use cost_per_instance)
                chosen = min(eligible, key=lambda c: c.get("tokens_in_mean", float("inf")))
                decisions.append(RoutingDecision(
                    task_type=task_type,
                    mode=mode,
                    chosen_recipient_id=chosen["recipient_id"],
                    expected_score=chosen.get("mean_score"),
                    ci_lo=chosen.get("ci_lo"),
                    ci_hi=chosen.get("ci_hi"),
                    reason=(
                        f"CI lower bound {chosen.get('ci_lo', 0):.3f} ≥ "
                        f"threshold {threshold:.3f}"
                        if threshold is not None else "no frontier baseline"
                    ),
                    routes_to_nosana=True,
                ))
            else:
                if frontier_cells:
                    fc = frontier_cells[0]
                    reason = (
                        f"No Nosana model met threshold (ratio={threshold_ratio})"
                        if nosana_cells
                        else "No Nosana cells available"
                    )
                    decisions.append(RoutingDecision(
                        task_type=task_type,
                        mode=mode,
                        chosen_recipient_id=fc["recipient_id"],
                        expected_score=fc.get("mean_score"),
                        ci_lo=fc.get("ci_lo"),
                        ci_hi=fc.get("ci_hi"),
                        reason=reason,
                        routes_to_nosana=False,
                    ))
                else:
                    decisions.append(RoutingDecision(
                        task_type=task_type,
                        mode=mode,
                        chosen_recipient_id=None,
                        expected_score=None,
                        ci_lo=None,
                        ci_hi=None,
                        reason="No recipients available",
                        routes_to_nosana=False,
                    ))
    return decisions


def compute_routable_share(
    decisions: list[RoutingDecision],
    instances_by_task: dict[str, int],
) -> dict[str, float]:
    """
    Return {"raw": X, "compiled": Y} — share of instances routable to Nosana.
    """
    shares: dict[str, float] = {}
    for mode in ("raw", "compiled"):
        mode_decisions = [d for d in decisions if d.mode == mode]
        total = sum(instances_by_task.values())
        routed = sum(
            instances_by_task.get(d.task_type, 0)
            for d in mode_decisions if d.routes_to_nosana
        )
        shares[mode] = routed / total if total > 0 else 0.0
    return shares


def save_routing_table(
    decisions: list[RoutingDecision],
    routable_shares: dict[str, float],
    path: Path,
) -> None:
    data = {
        "routable_share_raw": routable_shares.get("raw", 0.0),
        "routable_share_compiled": routable_shares.get("compiled", 0.0),
        "decisions": [
            {
                "task_type": d.task_type,
                "mode": d.mode,
                "chosen_recipient_id": d.chosen_recipient_id,
                "expected_score": d.expected_score,
                "ci_lo": d.ci_lo,
                "ci_hi": d.ci_hi,
                "reason": d.reason,
                "routes_to_nosana": d.routes_to_nosana,
            }
            for d in decisions
        ],
    }
    path.write_text(json.dumps(data, indent=2))
