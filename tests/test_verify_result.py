"""relay_verify.result: a COMPLETED job account is not enough; the result must show success."""

import json
from pathlib import Path

from relay_verify.result import check_result

FIX = Path(__file__).parent / "fixtures" / "phase0"


def _ok_result(**op):
    base = {"operationId": "relay-hello", "status": "success", "exitCode": 0, "logs": []}
    return {"status": "success", "startTime": 1, "endTime": 2, "opStates": [{**base, **op}]}


def test_real_failed_result_from_phase0_is_rejected():
    # Phase 0 hello attempt 1: on-chain COMPLETED, but the node could not fetch the job definition.
    res = json.loads((FIX / "hello_attempt1_result_raw.json").read_text())
    acct = json.loads((FIX / "hello_attempt1_account_decoded.json").read_text())
    assert acct["state_name"] == "COMPLETED"
    out = check_result(res)
    assert not out["ok"]
    assert any("no opStates" in r for r in out["reasons"])
    assert any("'failed'" in r for r in out["reasons"])


def test_success_result_passes():
    assert check_result(_ok_result())["ok"]


def test_nonzero_exit_code_fails():
    out = check_result(_ok_result(exitCode=1))
    assert not out["ok"] and out["exit_codes"] == [1]


def test_op_status_not_success_fails():
    assert not check_result(_ok_result(status="failed"))["ok"]


def test_errors_listed_fails():
    res = _ok_result()
    res["errors"] = ["something went wrong"]
    assert not check_result(res)["ok"]
