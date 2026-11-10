"""Read the batch outputs back out of a node-pinned Nosana result. Standard library only."""

from __future__ import annotations

import json


def stdout_lines(result: dict) -> list[str]:
    """All stdout text of every op, joined in order and split into lines."""
    text = "".join(entry.get("log") or "" for op in (result.get("opStates") or [])
                   for entry in (op.get("logs") or []) if entry.get("type") == "stdout")
    return text.splitlines()


def parse_outputs(result: dict) -> tuple[dict[str, dict], dict | None]:
    """{item_id: outputs.jsonl row} and the RELAY_SUMMARY object (None if absent)."""
    rows: dict[str, dict] = {}
    summary = None
    for line in stdout_lines(result):
        if line.startswith("RELAY_OUT "):
            row = json.loads(line[len("RELAY_OUT "):])
            if row["item_id"] in rows:
                raise ValueError(f"item {row['item_id']} appears twice in the result")
            rows[row["item_id"]] = row
        elif line.startswith("RELAY_SUMMARY "):
            summary = json.loads(line[len("RELAY_SUMMARY "):])
    return rows, summary


def regex_result(result: dict, name: str) -> str | None:
    """Group 1 of a job-definition `results` regex, as the node extracted it ([match, group1])."""
    for op in result.get("opStates") or []:
        val = (op.get("results") or {}).get(name)
        if isinstance(val, list) and len(val) >= 2:
            return val[1]
    return None


def container_times(result: dict) -> tuple[int | None, int | None]:
    """The container's start and end (ms since epoch) from the first op, as the node reports them."""
    ops = result.get("opStates") or []
    if not ops:
        return None, None
    return ops[0].get("startTime"), ops[0].get("endTime")
