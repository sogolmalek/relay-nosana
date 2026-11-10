"""relay-verify receipt.json [more.json ...] [--rpc URL] [--ipfs-gateway URL ...] [--json]

Exit 0: every receipt verified. 1: a check failed (named in the output). 2: couldn't check (network).
"""

from __future__ import annotations

import argparse
import json
import sys

from .checks import DEFAULT_GATEWAYS, Fetcher, verdict, verify
from .solana import DEFAULT_RPC


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="relay-verify", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("receipts", nargs="+")
    p.add_argument("--rpc", default=DEFAULT_RPC)
    p.add_argument("--ipfs-gateway", action="append", dest="gateways")
    p.add_argument("--json", action="store_true")
    a = p.parse_args(argv)
    f = Fetcher(a.rpc, a.gateways or DEFAULT_GATEWAYS)
    worst, report = 0, []
    for path in a.receipts:
        with open(path) as fh:
            receipt = json.load(fh)
        checks = verify(receipt, f)
        v = verdict(checks)
        if v == 1 or (v == 2 and worst == 0):
            worst = v
        report.append({"receipt": path, "item_id": receipt.get("item_id"), "grade": receipt.get("grade"),
                       "verdict": ["verified", "failed", "could not check"][v],
                       "checks": [c.to_dict() for c in checks]})
        if not a.json:
            print(f"{path}: {report[-1]['verdict'].upper()} ({receipt.get('grade')}, {receipt.get('item_id')})")
            for c in checks:
                mark = {True: "PASS", False: "FAIL", None: "N/A "}[c.ok]
                print(f"  [{mark}] {c.id:>10}  {c.name}" + (f": {c.reason}" if c.reason else ""))
    if a.json:
        print(json.dumps(report, indent=2))
    return worst


if __name__ == "__main__":
    sys.exit(main())
