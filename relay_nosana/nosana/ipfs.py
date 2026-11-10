"""Pin exact bytes so Nosana nodes can fetch them, and fetch them back verified.

Phase 0: nodes fetch job definitions only through nosana.mypinata.cloud, which serves only CIDs
pinned to Nosana's Pinata account. So every file is pinned twice with pinFileToIPFS (CIDv0):
to Nosana's account with the community key @nosana/ipfs ships as a public default (read at runtime,
never logged or committed), and to ours (PINATA_JWT) for durability. Both must return our local CID.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from relay_verify.cid import cidv0_of_bytes

PINATA_API = "https://api.pinata.cloud"
ROOT = Path(__file__).resolve().parents[2]
NOSANA_IPFS_DEFAULTS = ROOT / ".nosana-npm" / "node_modules" / "@nosana" / "ipfs" / "dist" / "defaults" / "index.js"
MAX_SINGLE_BLOCK = 262144  # CIDv0 recomputable from the bytes only up to one 256 KiB block


class PinError(RuntimeError):
    pass


def nosana_ipfs_jwt() -> str:
    v = os.environ.get("NOSANA_IPFS_JWT", "").strip()
    if v:
        return v
    if NOSANA_IPFS_DEFAULTS.exists():
        m = re.search(r"jwt:\s*['\"]([^'\"]+)['\"]", NOSANA_IPFS_DEFAULTS.read_text())
        if m:
            return m.group(1)
    raise PinError("Nosana IPFS key not found: set NOSANA_IPFS_JWT or "
                   "`npm install --prefix .nosana-npm --ignore-scripts @nosana/ipfs@2.24.3`")


def own_jwt() -> str:
    v = os.environ.get("PINATA_JWT", "").strip()
    if not v:
        raise PinError("PINATA_JWT is not set")
    return v


def _pin_file(data: bytes, name: str, jwt: str) -> tuple[int, dict]:
    boundary = uuid.uuid4().hex
    parts = []

    def part(field, value, filename=None):
        disp = f'form-data; name="{field}"' + (f'; filename="{filename}"' if filename else "")
        parts.append(f"--{boundary}\r\nContent-Disposition: {disp}\r\nContent-Type: application/json\r\n\r\n".encode()
                     + value + b"\r\n")

    part("file", data, filename=name)
    part("pinataOptions", json.dumps({"cidVersion": 0, "wrapWithDirectory": False}).encode())
    part("pinataMetadata", json.dumps({"name": name}).encode())
    body = b"".join(parts) + f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{PINATA_API}/pinning/pinFileToIPFS", data=body, method="POST", headers={
        "Authorization": f"Bearer {jwt}", "Content-Type": f"multipart/form-data; boundary={boundary}",
        "User-Agent": "relay-nosana/0.2"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode(errors="replace")[:500]}


def pin_both(data: bytes, name: str) -> dict:
    """Pin to Nosana's and our Pinata accounts; returns the CID and both responses. Raises on mismatch."""
    if len(data) > MAX_SINGLE_BLOCK:
        raise PinError(f"{name} is {len(data)} bytes; keep pinned files under 256 KiB (one IPFS block)")
    local = cidv0_of_bytes(data)
    out = {"local_cidv0": local}
    for label, jwt in (("nosana", nosana_ipfs_jwt()), ("own", own_jwt())):
        s, resp = _pin_file(data, name, jwt)
        out[label] = {"http_status": s, "body": resp}
        if s != 200 or resp.get("IpfsHash") != local:
            raise PinError(f"{label} pin of {name}: HTTP {s}, CID {resp.get('IpfsHash')} vs local {local}")
    out["cid"] = local
    return out
