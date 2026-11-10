"""Merkle tree over batch outputs (receipt spec v0.1). Standard library only.

leaf = sha256(0x00 || item_id utf-8 || input_sha256 (32 bytes) || output_sha256 (32 bytes))
node = sha256(0x01 || left || right)
Leaves sorted by item_id (Python string order = Unicode code points). An odd last node is promoted
unchanged. A path entry is {"side": "L"|"R", "hash": hex}: the sibling's side.
"""

from __future__ import annotations

import hashlib


def leaf_hash(item_id: str, input_sha256: str, output_sha256: str) -> bytes:
    return hashlib.sha256(b"\x00" + item_id.encode("utf-8") + bytes.fromhex(input_sha256)
                          + bytes.fromhex(output_sha256)).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    return hashlib.sha256(b"\x01" + left + right).digest()


def _levels(leaves: list[bytes]) -> list[list[bytes]]:
    if not leaves:
        raise ValueError("empty tree")
    levels = [leaves]
    while len(levels[-1]) > 1:
        cur = levels[-1]
        nxt = [node_hash(cur[i], cur[i + 1]) for i in range(0, len(cur) - 1, 2)]
        if len(cur) % 2:
            nxt.append(cur[-1])
        levels.append(nxt)
    return levels


def sorted_leaves(items: list[dict]) -> tuple[list[str], list[bytes]]:
    """items: dicts with item_id, input_sha256, output_sha256. Returns (ids, leaves) sorted by id."""
    ids = [it["item_id"] for it in items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate item_id")
    rows = sorted(items, key=lambda it: it["item_id"])
    return ([r["item_id"] for r in rows],
            [leaf_hash(r["item_id"], r["input_sha256"], r["output_sha256"]) for r in rows])


def root(items: list[dict]) -> str:
    return _levels(sorted_leaves(items)[1])[-1][0].hex()


def path(items: list[dict], item_id: str) -> list[dict]:
    ids, leaves = sorted_leaves(items)
    i = ids.index(item_id)
    out = []
    for level in _levels(leaves)[:-1]:
        if i % 2 == 1:
            out.append({"side": "L", "hash": level[i - 1].hex()})
        elif i + 1 < len(level):
            out.append({"side": "R", "hash": level[i + 1].hex()})
        i //= 2
    return out


def root_from_path(leaf: bytes, merkle_path: list[dict]) -> str:
    h = leaf
    for step in merkle_path:
        sib = bytes.fromhex(step["hash"])
        if step["side"] == "L":
            h = node_hash(sib, h)
        elif step["side"] == "R":
            h = node_hash(h, sib)
        else:
            raise ValueError(f"bad side {step['side']!r}")
    return h.hex()
