"""Pure dashboard credential and trusted-device helpers; no application state."""

import base64
import binascii
import hashlib
import hmac
import re
import secrets
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from itsdangerous import BadData, URLSafeSerializer
from werkzeug.security import check_password_hash, generate_password_hash

OPERATOR_PATTERN = re.compile(r"[a-z0-9_.-]{1,32}", re.ASCII)


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _decode_secret(secret_b32: str) -> bytes:
    try:
        decoded = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8), casefold=True)
        if not decoded:
            raise ValueError("Empty TOTP secret")
        return decoded
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid TOTP secret") from exc


def totp_at(secret_b32: str, step: int, digits: int = 6) -> str:
    digest = hmac.new(_decode_secret(secret_b32), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 15
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff
    return str(value % (10 ** digits)).zfill(digits)


def verify_totp(secret_b32, code, now: float, *, window: int = 1, step_seconds: int = 30) -> int | None:
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    current = int(now // step_seconds)
    for step in range(max(0, current - window), current + window + 1):
        if hmac.compare_digest(totp_at(secret_b32, step), code):
            return step
    return None


def provisioning_uri(operator, secret_b32, issuer="Planet Express") -> str:
    label = quote(f"{issuer}:{operator}", safe="")
    query = urlencode({"secret": secret_b32, "issuer": issuer, "algorithm": "SHA1",
                       "digits": 6, "period": 30})
    return f"otpauth://totp/{label}?{query}"


def hash_passphrase(passphrase) -> str:
    if len(passphrase) < 12:
        raise ValueError("Passphrase must contain at least 12 characters")
    return generate_password_hash(passphrase)


def verify_passphrase(hash_, passphrase) -> bool:
    try:
        return check_password_hash(hash_, passphrase)
    except Exception:  # noqa: BLE001 -- malformed stored credentials must fail closed
        return False


@dataclass(frozen=True)
class Operator:
    name: str
    passphrase_hash: str
    totp_secret: str


def load_operators(environ: Mapping[str, str]) -> dict[str, Operator]:
    raw = environ.get("PE_OPERATORS", "")
    if not raw.strip():
        return {}
    names = [name.strip().casefold() for name in raw.split(",")]
    if len(names) > 3 or len(set(names)) != len(names) or any(
        OPERATOR_PATTERN.fullmatch(name) is None for name in names
    ):
        raise ValueError("Invalid PE_OPERATORS")
    prefixes = {}
    for name in names:
        prefix = "PE_OPERATOR_" + name.upper().replace(".", "_").replace("-", "_")
        if prefix in prefixes:
            # e.g. alice-bob and alice.bob would silently share one passphrase and TOTP secret
            # while keeping separate lockouts and device epochs (Codex review, T13a).
            raise ValueError(f"Operators {prefixes[prefix]!r} and {name!r} map to the same {prefix}_* variables")
        prefixes[prefix] = name
    result = {}
    for name in names:
        prefix = "PE_OPERATOR_" + name.upper().replace(".", "_").replace("-", "_")
        hash_var, secret_var = prefix + "_PASSPHRASE_HASH", prefix + "_TOTP_SECRET"
        hash_ = environ.get(hash_var, "")
        try:
            method, salt, digest = hash_.split("$")
            if not salt or not digest or re.fullmatch(r"[0-9a-f]+", digest) is None:
                raise ValueError
            # Let Werkzeug validate the algorithm and its parameters.
            expected = generate_password_hash("", method=method).rsplit("$", 1)[1]
            if len(digest) != len(expected):
                raise ValueError
        except Exception:  # noqa: BLE001 -- invalid configuration must name only the variable
            raise ValueError(f"Invalid {hash_var}") from None
        secret = environ.get(secret_var, "")
        try:
            _decode_secret(secret)
        except ValueError:
            raise ValueError(f"Invalid {secret_var}") from None
        result[name] = Operator(name, hash_, secret)
    return result


def make_device_token(secret_key: str, operator: str, epoch: int, now: float) -> str:
    return URLSafeSerializer(secret_key, salt="pe-trusted-device").dumps(
        {"op": operator, "ep": epoch, "iat": int(now)}
    )


def read_device_token(secret_key, token, now, *, max_age=30*86400) -> tuple[str, int] | None:
    try:
        payload = URLSafeSerializer(secret_key, salt="pe-trusted-device").loads(token)
    except (BadData, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"op", "ep", "iat"}:
        return None
    op, epoch, issued = payload["op"], payload["ep"], payload["iat"]
    if (not isinstance(op, str) or OPERATOR_PATTERN.fullmatch(op) is None
            or type(epoch) is not int or epoch < 0 or type(issued) is not int
            or issued > now + 60 or now - issued > max_age):
        return None
    return op, epoch
