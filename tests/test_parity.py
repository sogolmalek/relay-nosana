"""Browser verifier (docs/app/verifier.js, run in Node) vs the Python CLI: same verdict, same checks, same
reasons, on every published receipt and every tamper, over recorded RPC/IPFS fixtures (no network)."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess

import pytest

from relay_verify.checks import Unavailable, verdict, verify
from relay_verify.solana import job_from_rpc_response

from .parity.cases import FIXTURES, ROOT, all_cases

pytestmark = pytest.mark.skipif(shutil.which("node") is None or not (FIXTURES / "cache.json").exists(),
                                reason="needs node and tests/fixtures/parity/cache.json")


class CacheFetcher:
    rpc = "cache://rpc"

    def __init__(self, cache):
        self.c = cache

    def transaction(self, sig):
        if sig not in self.c["tx"]:
            raise Unavailable(f"not in cache: tx {sig}")
        return self.c["tx"][sig]

    def account(self, address):
        if address not in self.c["acct"]:
            raise Unavailable(f"not in cache: account {address}")
        return job_from_rpc_response(address, self.c["acct"][address])

    def ipfs(self, cid):
        if cid not in self.c["ipfs"]:
            raise Unavailable(f"not in cache: ipfs {cid}")
        return base64.b64decode(self.c["ipfs"][cid])


def test_browser_verifier_matches_python_cli(tmp_path):
    cases = all_cases()
    (tmp_path / "cases.json").write_text(json.dumps(cases))
    p = subprocess.run(["node", str(ROOT / "tests" / "parity" / "verify_node.mjs"), str(tmp_path / "cases.json"),
                       str(FIXTURES / "cache.json")], capture_output=True, text=True, timeout=600)
    assert p.returncode == 0, p.stderr
    js = {x["name"]: x for x in json.loads(p.stdout)}
    cache = json.loads((FIXTURES / "cache.json").read_text())
    verdicts = {}
    for name, r in cases:
        cks = verify(r, CacheFetcher(cache))
        py = [c.to_dict() for c in cks]
        assert js[name]["checks"] == py, name
        assert js[name]["verdict"] == verdict(cks), name
        verdicts[name] = verdict(cks)
    published = [n for n, _ in cases if not n.startswith(("tamper/", "v0.1/"))]
    assert len(published) == 72 and all(verdicts[n] == 0 for n in published)
    expect = {"decision": {"5"}, "output_only": {"receipt_id"}, "model": {"3"}, "job": {"1"}, "post_tx": {"1"},
              "merkle": {"5"}, "node": {"6"}, "pending_rerun": set()}
    for name, r in cases:
        if name.startswith("tamper/"):
            kind = name.split("/")[-1]
            failing = {c["id"] for c in js[name]["checks"] if c["ok"] is False}
            if kind == "unknown_finish":
                assert verdicts[name] == 2
            elif kind == "pending_rerun":
                assert verdicts[name] == 0
            else:
                assert expect[kind] <= failing, (name, failing)
    assert verdicts["v0.1/P-0101"] == 1
