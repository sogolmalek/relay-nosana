"""Check a Nosana job result (the JSON the node pins to IPFS; its CID is the account's ipfsResult).

Phase 0 finding: an on-chain state of COMPLETED does not mean the container ran. The first hello
job reached COMPLETED with a result whose status was "failed", opStates empty, and an error saying
the node could not fetch the job definition. So the verifier checks the result itself too.
"""

from __future__ import annotations

SUCCESS = "success"


def check_result(result: dict) -> dict:
    """Return {"ok": bool, "status": ..., "exit_codes": [...], "reasons": [...]}.

    ok only if the result status is "success", there is at least one op, every op's status is
    "success" with exit code 0, and the result lists no errors.
    """
    reasons: list[str] = []
    status = result.get("status")
    if status != SUCCESS:
        reasons.append(f"result status is {status!r}, not {SUCCESS!r}")
    errors = result.get("errors") or []
    if errors:
        reasons.append(f"result lists {len(errors)} error(s): {str(errors[0])[:200]}")
    ops = result.get("opStates") or []
    if not ops:
        reasons.append("result has no opStates: no container ran")
    exit_codes = []
    for op in ops:
        oid = op.get("operationId")
        code = op.get("exitCode")
        exit_codes.append(code)
        if code != 0:
            reasons.append(f"op {oid!r} exit code {code!r}")
        if op.get("status") != SUCCESS:
            reasons.append(f"op {oid!r} status {op.get('status')!r}")
    return {"ok": not reasons, "status": status, "exit_codes": exit_codes, "reasons": reasons}
