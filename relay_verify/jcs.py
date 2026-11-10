"""RFC 8785 JSON Canonicalization Scheme (JCS). Standard library only.

Keys sorted by UTF-16 code units, no whitespace, strings escaped minimally (only `"`, `\\` and
control characters), numbers in ECMAScript Number.prototype.toString form. Integers must be
within ±2**53 so every implementation reads them the same way.

batch_runner/run.py carries a copy of `canonical` (it runs inside the container with no Relay
code); tests/test_receipts.py checks that both produce the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import math
from decimal import Decimal

_MAX_SAFE = 2**53


def _num(x) -> str:
    if isinstance(x, bool):
        raise TypeError("bool is not a number")
    if isinstance(x, int):
        if abs(x) > _MAX_SAFE:
            raise ValueError(f"integer {x} outside ±2**53")
        return str(x)
    if not math.isfinite(x):
        raise ValueError("NaN and Infinity are not JSON")
    if x == 0:
        return "0"
    if x.is_integer() and abs(x) < 1e21:
        return str(int(x))
    sign = "-" if x < 0 else ""
    t = Decimal(repr(abs(x))).as_tuple()
    digits = "".join(map(str, t.digits)).rstrip("0") or "0"
    k = len(digits)
    n = t.exponent + len(t.digits)  # value = 0.d1..dk × 10**n
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        s = digits[0] + ("." + digits[1:] if k > 1 else "") + "e" + ("+" if e > 0 else "-") + str(abs(e))
    return sign + s


def _ser(v, out: list[str]) -> None:
    if v is None:
        out.append("null")
    elif v is True:
        out.append("true")
    elif v is False:
        out.append("false")
    elif isinstance(v, (int, float)):
        out.append(_num(v))
    elif isinstance(v, str):
        out.append(json.dumps(v, ensure_ascii=False))
    elif isinstance(v, (list, tuple)):
        out.append("[")
        for i, item in enumerate(v):
            if i:
                out.append(",")
            _ser(item, out)
        out.append("]")
    elif isinstance(v, dict):
        out.append("{")
        for i, k in enumerate(sorted(v, key=lambda s: s.encode("utf-16-be"))):
            if not isinstance(k, str):
                raise TypeError("object keys must be strings")
            if i:
                out.append(",")
            out.append(json.dumps(k, ensure_ascii=False))
            out.append(":")
            _ser(v[k], out)
        out.append("}")
    else:
        raise TypeError(f"not JSON: {type(v).__name__}")


def canonical(obj) -> bytes:
    out: list[str] = []
    _ser(obj, out)
    return "".join(out).encode("utf-8")


def sha256_hex(obj) -> str:
    return hashlib.sha256(canonical(obj)).hexdigest()
