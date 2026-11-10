"""CIDv0 for single-block UnixFS files, and the 32-byte form Nosana stores on Solana.

Nosana stores `ipfsJob` / `ipfsResult` as the 32-byte sha2-256 digest of a CIDv0
(base58(0x12 0x20 || digest)). For a file up to the default chunk size (256 KiB),
`ipfs add` / Pinata produce one dag-pb node:

    PBNode { Data: UnixFS { Type: File, Data: <bytes>, filesize: len } }

so the CID can be recomputed from the raw bytes. A gateway that serves different
bytes can't produce a matching CID.
"""

from __future__ import annotations

import hashlib

from .b58 import b58decode, b58encode

CHUNK_SIZE = 262144  # default chunker; above this the DAG has several blocks


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _field_bytes(field: int, data: bytes) -> bytes:
    return _varint((field << 3) | 2) + _varint(len(data)) + data


def _field_varint(field: int, value: int) -> bytes:
    return _varint(field << 3) + _varint(value)


def unixfs_file_block(data: bytes) -> bytes:
    """dag-pb block for a single-chunk UnixFS file."""
    if len(data) > CHUNK_SIZE:
        raise ValueError(f"{len(data)} bytes is more than one chunk ({CHUNK_SIZE}); multi-block DAGs are not supported")
    unixfs = _field_varint(1, 2)  # Type = File
    if data:
        unixfs += _field_bytes(2, data)
    unixfs += _field_varint(3, len(data))  # filesize
    return _field_bytes(1, unixfs)  # PBNode.Data (no links)


def digest_to_cid(digest: bytes) -> str:
    if len(digest) != 32:
        raise ValueError("expected a 32-byte sha2-256 digest")
    return b58encode(b"\x12\x20" + digest)


def cid_to_digest(cid: str) -> bytes:
    raw = b58decode(cid)
    if len(raw) != 34 or raw[0] != 0x12 or raw[1] != 0x20:
        raise ValueError(f"not a CIDv0 sha2-256 hash: {cid}")
    return raw[2:]


MAX_LINKS = 174  # kubo's balanced layout: links per node; one level covers 174 x 256 KiB (~45 MB)


def _pb_link(digest: bytes, tsize: int) -> bytes:
    """PBLink: Hash (CIDv0 multihash bytes), Name (empty), Tsize (the linked block's size)."""
    return _field_bytes(1, b"\x12\x20" + digest) + _field_bytes(2, b"") + _field_varint(3, tsize)


def unixfs_root_block(data: bytes) -> bytes:
    """dag-pb root of a multi-chunk UnixFS file, balanced layout, one level: the links come before
    Data (dag-pb canonical order); Data is UnixFS File with filesize and each chunk's size."""
    chunks = [data[i:i + CHUNK_SIZE] for i in range(0, len(data), CHUNK_SIZE)]
    if len(chunks) > MAX_LINKS:
        raise ValueError(f"{len(data)} bytes needs more than one DAG level; not supported")
    links, sizes = b"", b""
    for ch in chunks:
        leaf = unixfs_file_block(ch)
        links += _field_bytes(2, _pb_link(hashlib.sha256(leaf).digest(), len(leaf)))
        sizes += _field_varint(4, len(ch))
    unixfs = _field_varint(1, 2) + _field_varint(3, len(data)) + sizes
    return links + _field_bytes(1, unixfs)


def cidv0_of_bytes(data: bytes) -> str:
    """CIDv0 that `ipfs add --cid-version=0` (default 256 KiB chunker, balanced layout, no raw leaves)
    gives for `data` as a single file. Above one chunk, checked against Pinata (tests/fixtures/phase1b)."""
    block = unixfs_file_block(data) if len(data) <= CHUNK_SIZE else unixfs_root_block(data)
    return digest_to_cid(hashlib.sha256(block).digest())
