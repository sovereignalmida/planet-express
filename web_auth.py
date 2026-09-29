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


# ── Elevated sessions (T47) ──────────────────────────────────────────────────────
# An operator signed in can run R0 and R1. Originating an R2 or R3 -- taking a stack down,
# writing a compose file -- asks them to re-enter their passphrase first, and that grants a
# short window. Sudo's bargain: prove again, briefly, that it is you.
#
# The passphrase and not TOTP, deliberately. TOTP means reaching for the phone, and the point
# of this work is that the phone stops being the only way to authorise anything. The threat
# here is a browser someone walked up to while it held a 30-day trust cookie, and re-entering
# the passphrase is what answers that.

# The window, refreshed by each elevated action...
ELEVATION_SECONDS = 600
# ...and the absolute cap from the moment the passphrase was entered, so a long editing
# session cannot refresh its way into being permanently elevated.
ELEVATION_CAP_SECONDS = 3600


def _elevation_serializer(secret_key: str) -> URLSafeSerializer:
    # Its own salt: a marker must never be readable as, or forgeable from, a device token.
    return URLSafeSerializer(secret_key, salt="pe-elevation")


def credential_fingerprint(credential: str) -> str:
    """What a marker binds to so a passphrase reset invalidates it without anyone acting."""
    return hashlib.sha256(f"pe-credential:{credential}".encode()).hexdigest()


def token_fingerprint(token: str) -> str:
    """What an elevation marker is bound to, so it is worthless on another session."""
    return hashlib.sha256(token.encode()).hexdigest()


def make_elevation(secret_key: str, operator: str, epoch: int, token: str, now: float,
                   *, credential: str, first: float | None = None) -> str:
    """Mint (or refresh) an elevation marker.

    `first` is the moment the passphrase was actually entered, carried through every refresh so
    the cap is measured from it. The cap is derived from `first` rather than stored, so a
    refresh cannot quietly extend it.

    `credential` is the operator's stored passphrase hash. Binding to it makes a reset revoke
    every outstanding marker by itself: the hash is re-salted on every change, so a marker
    minted under the old one stops matching. The alternative was to rely on someone also
    running the separate device-revocation script, which `dashboard_operators.py reset` only
    suggests -- and a security boundary that depends on remembering a second command is one
    that is off whenever it matters.

    Keyword-only and with no default on purpose: a caller that forgets it should not compile.
    """
    return _elevation_serializer(secret_key).dumps({
        "op": operator,
        "ep": epoch,
        "tk": token_fingerprint(token),
        "pw": credential_fingerprint(credential),
        "first": int(first if first is not None else now),
        "exp": int(now) + ELEVATION_SECONDS,
    })


def read_elevation(secret_key, marker, token, now, *, credential) -> tuple[str, int, int] | None:
    """(operator, epoch, first) for a valid marker, or None.

    Fails closed on everything: a bad signature, a payload that is not exactly this shape, a
    marker minted for another session's token, an expired window, or one past its cap. The
    caller still has to check the operator is known and its epoch current -- same as
    read_device_token, whose answer means "this token is well-formed", not "this is allowed".
    """
    try:
        payload = _elevation_serializer(secret_key).loads(marker)
    except (BadData, TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or set(payload) != {"op", "ep", "tk", "pw", "first", "exp"}:
        return None
    operator, epoch, fingerprint, credential_seen, first, expires = (
        payload["op"], payload["ep"], payload["tk"], payload["pw"],
        payload["first"], payload["exp"])
    if (not isinstance(operator, str) or OPERATOR_PATTERN.fullmatch(operator) is None
            or type(epoch) is not int or epoch < 0
            or not isinstance(fingerprint, str) or not isinstance(credential_seen, str)
            or type(first) is not int or type(expires) is not int):
        return None
    # hmac.compare_digest, not ==: this is a secret-derived value being compared.
    if not hmac.compare_digest(fingerprint, token_fingerprint(token)):
        return None
    if not hmac.compare_digest(credential_seen, credential_fingerprint(credential)):
        return None
    if (first > now + 60                      # minted in the future: clock skew, or forged
            or now > expires                  # the sliding window has run out
            or now > first + ELEVATION_CAP_SECONDS   # the absolute cap has
            or expires > now + ELEVATION_SECONDS):   # a window longer than we ever mint
        return None
    return operator, epoch, first
