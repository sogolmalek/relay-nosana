"""Record every Solana RPC and IPFS response the parity cases need (network; run once, commit the cache).

    .venv/bin/python -m tests.parity.record
"""

from __future__ import annotations

import base64
import json

from relay_verify.checks import Fetcher, verify

from .cases import FIXTURES, all_cases


class Recording(Fetcher):
    def __init__(self):
        super().__init__("https://solana-rpc.publicnode.com")
        self.log = {"tx": {}, "acct": {}, "ipfs": {}}

    def transaction(self, sig):
        tx = super().transaction(sig)
        self.log["tx"][sig] = tx
        return tx

    def account(self, address):
        if address not in self.log["acct"]:
            self.log["acct"][address] = self._rpc("getAccountInfo", [address, {"encoding": "base64",
                                                                               "commitment": "confirmed"}])
        return super().account(address)

    def ipfs(self, cid):
        data = super().ipfs(cid)
        self.log["ipfs"][cid] = base64.b64encode(data).decode()
        return data


def main():
    f = Recording()
    for name, r in all_cases():
        cks = verify(r, f)
        print(name, [(c.id, c.ok) for c in cks if c.ok is not True], flush=True)
    FIXTURES.mkdir(parents=True, exist_ok=True)
    (FIXTURES / "cache.json").write_text(json.dumps(f.log, sort_keys=True) + "\n")
    print({k: len(v) for k, v in f.log.items()})


if __name__ == "__main__":
    main()
