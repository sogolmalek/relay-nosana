"""Frontier lane: Claude Opus 5.5 on the same input lines, structured JSON output.

Claude Opus 5.5 rejects `temperature` and always thinks, so this lane is as deterministic as the API
allows (effort "low", a JSON-schema output format), not seed-fixed like the open model. The
server-side refusal fallback is on; every call records which model actually answered.
"""

from __future__ import annotations

import datetime as dt
import json
import time

MODEL = "claude-opus-5-5"
PRICE_IN, PRICE_OUT = 4.00, 20.00  # USD per million tokens, Claude Opus 5.5


def call(client, line: dict, schema: dict) -> dict:
    import anthropic

    system = next(m["content"] for m in line["messages"] if m["role"] == "system")
    msgs = [m for m in line["messages"] if m["role"] != "system"]
    t0 = time.time()
    rec = {"item_id": line["item_id"], "requested_model": MODEL,
           "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")}
    try:
        resp = client.beta.messages.create(
            model=MODEL, max_tokens=2048, system=system, messages=msgs,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
    except anthropic.APIStatusError as e:
        rec.update(error=f"HTTP {e.status_code}: {e.message}", output=None, latency_ms=(time.time() - t0) * 1000)
        return rec
    except anthropic.APIConnectionError as e:
        rec.update(error=f"connection: {e}", output=None, latency_ms=(time.time() - t0) * 1000)
        return rec
    text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
    rec.update(request_id=resp._request_id, served_model=resp.model, stop_reason=resp.stop_reason,
               tokens_in=resp.usage.input_tokens, tokens_out=resp.usage.output_tokens, output_raw=text,
               latency_ms=(time.time() - t0) * 1000,
               cost_usd=(resp.usage.input_tokens * PRICE_IN + resp.usage.output_tokens * PRICE_OUT) / 1e6)
    if resp.stop_reason == "refusal":
        rec.update(error="refusal", output=None)
        return rec
    try:
        out = json.loads(text)
        ok = isinstance(out, dict) and set(out) == set(schema["properties"])
        rec.update(output=out if ok else None, error=None if ok else "output does not match the schema")
    except json.JSONDecodeError:
        rec.update(output=None, error="not JSON")
    return rec
