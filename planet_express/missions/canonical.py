"""RFC 8785-style canonical JSON for hashed mission models.

Only the types a hashed model may contain are allowed: dict (str keys), list/tuple, str, bool, None and
int within the IEEE-754 safe range. Floats are rejected outright, because JCS number formatting is the
one place two implementations legitimately disagree and no hashed model needs a fraction. Object keys
sort by UTF-16 code units as RFC 8785 requires. Lone surrogates (not encodable as UTF-8) are rejected.
"""

import hashlib
import json
from typing import Any

MAX_SAFE_INT = 2**53 - 1


class CanonicalError(ValueError):
    """The value cannot be canonicalised unambiguously."""


def _check_str(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise CanonicalError("string is not valid UTF-8 (lone surrogate)") from exc
    return value


def _emit(value: Any, out: list[str]) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > MAX_SAFE_INT:
            raise CanonicalError("integer outside the safe range")
        out.append(str(int(value)))
    elif isinstance(value, float):
        raise CanonicalError("floats are not allowed in hashed models")
    elif isinstance(value, str):
        out.append(json.dumps(_check_str(value), ensure_ascii=False))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _emit(item, out)
        out.append("]")
    elif isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise CanonicalError("object keys must be strings")
        out.append("{")
        for i, key in enumerate(sorted(value, key=lambda k: _check_str(k).encode("utf-16-be"))):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _emit(value[key], out)
        out.append("}")
    else:
        raise CanonicalError(f"unsupported type {type(value).__name__}")


def canonical_json(value: Any) -> str:
    out: list[str] = []
    _emit(value, out)
    return "".join(out)


def canonical_bytes(value: Any) -> bytes:
    return canonical_json(value).encode("utf-8")


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict:
    seen: dict[str, Any] = {}
    for key, val in pairs:
        if key in seen:
            raise CanonicalError(f"duplicate key {key!r}")
        seen[key] = val
    return seen


def _reject_float(text: str) -> Any:
    raise CanonicalError(f"floats are not allowed: {text}")


def _reject_constant(text: str) -> Any:
    raise CanonicalError(f"{text} is not allowed")


def loads_strict(data: str | bytes) -> Any:
    """Parse JSON that is about to be hashed: no duplicate keys, floats, NaN/Infinity or bad UTF-8."""
    if isinstance(data, bytes | bytearray):
        try:
            data = bytes(data).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CanonicalError("input is not valid UTF-8") from exc
    try:
        value = json.loads(
            data, object_pairs_hook=_no_duplicates,
            parse_float=_reject_float, parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise CanonicalError(f"invalid JSON: {exc}") from exc
    canonical_json(value)  # surfaces out-of-range ints and lone surrogates
    return value


def sha256_hex(value: Any) -> str:
    """SHA-256 of the canonical JSON of `value`."""
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
