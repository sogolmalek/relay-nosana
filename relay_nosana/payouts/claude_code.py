"""Declared lane (Phase 1): headless Claude Code on the user's own login, `claude -p --output-format json`.

No API key and no API credits: the call runs under the existing Claude Code login. The `total_cost_usd`
in the JSON output is Claude Code's list-price figure, not a charge, so it is recorded as
`notional_cost_usd` and the receipt's `cost_usd` is 0.

Each item is one fresh session: the item's system message as `--system-prompt`, its user message on
stdin, the output schema as `--json-schema`, no tools, no slash commands, no MCP servers, no session
saved, run from an empty working directory so no project CLAUDE.md is picked up.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import tempfile
import time

NAME = "claude-code"
FLAGS = ["--output-format", "json", "--tools", "", "--disable-slash-commands", "--strict-mcp-config",
         "--no-session-persistence", "--effort", "low"]


def _cli_version() -> str | None:
    try:
        return subprocess.run(["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def reported_model(out: dict) -> str | None:
    """The model as the JSON output reports it: modelUsage's canonicalModel (or its key)."""
    mu = out.get("modelUsage") or {}
    names = [(v or {}).get("canonicalModel") or k for k, v in mu.items()]
    return ",".join(sorted(set(names))) or None


def call(line: dict, schema: dict, timeout_s: int = 300, cwd: str | None = None) -> dict:
    msgs = {m["role"]: m["content"] for m in line["messages"]}
    rec: dict = {"item_id": line["item_id"], "provider": NAME, "cli_version": _cli_version(),
                 "flags": FLAGS, "output": None, "error": None}
    cmd = ["claude", "-p", *FLAGS, "--system-prompt", msgs["system"], "--json-schema", json.dumps(schema)]
    t0 = time.time()
    with tempfile.TemporaryDirectory(prefix="relay-cc-") as tmp:
        try:
            p = subprocess.run(cmd, input=msgs["user"], capture_output=True, text=True, timeout=timeout_s,
                               cwd=cwd or tmp)
        except subprocess.TimeoutExpired:
            rec.update(error=f"timeout after {timeout_s}s", latency_ms=int((time.time() - t0) * 1000))
            return rec
    rec["latency_ms"] = int((time.time() - t0) * 1000)
    rec["timestamp"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rec["exit_code"] = p.returncode
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError:
        rec["error"] = f"exit {p.returncode}, stdout not JSON: {p.stdout[:300]!r} stderr: {p.stderr[:300]!r}"
        return rec
    usage = out.get("usage") or {}
    rec.update(session_id=out.get("session_id"), model=reported_model(out), subtype=out.get("subtype"),
               is_error=out.get("is_error"), num_turns=out.get("num_turns"), result_text=out.get("result"),
               tokens_in=(usage.get("input_tokens") or 0) + (usage.get("cache_creation_input_tokens") or 0)
               + (usage.get("cache_read_input_tokens") or 0),
               tokens_out=usage.get("output_tokens"), notional_cost_usd=out.get("total_cost_usd"),
               duration_ms=out.get("duration_ms"))
    so = out.get("structured_output")
    if out.get("is_error") or out.get("subtype") != "success":
        rec["error"] = f"{out.get('subtype')}: {str(out.get('result'))[:300]}"
    elif not isinstance(so, dict):
        rec["error"] = "no structured_output"
    else:
        props = schema.get("properties") or {}
        bad = set(so) != set(props) or any("enum" in s and so[k] not in s["enum"] for k, s in props.items())
        if bad:
            rec["error"] = f"structured_output off schema: {so}"
        else:
            rec["output"] = so
    return rec
