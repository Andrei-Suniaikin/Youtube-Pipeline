"""Explicit local credentials and RFC 6238 SHA-1 TOTP; no CLI or network I/O.

JSON schema: {"accounts": [{"login": "exact email", "password": "...", "totp": "Base32"}]}.
The caller supplies the path and login. Nothing is discovered or logged.
"""

import base64
import binascii
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
from pathlib import Path
import re
import time


class AuthSecretsError(ValueError):
    """A value-free diagnostic safe to display to the operator."""


def _valid_login(value: object) -> bool:
    return (isinstance(value, str) and value.count("@") == 1
            and all(value.split("@")) and not any(c.isspace() for c in value))


def _decode_totp(secret: str) -> bytes:
    if not isinstance(secret, str) or not re.fullmatch(r"[A-Za-z2-7]+={0,6}", secret):
        raise AuthSecretsError("Invalid TOTP Base32 secret")
    raw = secret.upper().rstrip("=")
    padded = raw + "=" * (-len(raw) % 8)
    if "=" in secret and secret.upper() != padded:
        raise AuthSecretsError("Invalid TOTP Base32 secret")
    try:
        decoded = base64.b32decode(padded)
    except (ValueError, binascii.Error):
        raise AuthSecretsError("Invalid TOTP Base32 secret") from None
    if not decoded or base64.b32encode(decoded).decode("ascii").rstrip("=") != raw:
        raise AuthSecretsError("Invalid TOTP Base32 secret")
    return decoded


@dataclass(frozen=True)
class Credentials:
    login: str = field(repr=False)
    password: str = field(repr=False)
    totp: str = field(repr=False)

    def __post_init__(self) -> None:
        if not _valid_login(self.login):
            raise AuthSecretsError("Invalid account login")
        if not isinstance(self.password, str) or not self.password:
            raise AuthSecretsError("Missing or invalid account password")
        _decode_totp(self.totp)


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise AuthSecretsError("Duplicate JSON field")
        result[key] = value
    return result


def load_credentials(secret_file: str | Path, login: str) -> Credentials:
    """Read a configured file and return exactly one case-sensitive email match.

    Base32 accepts upper/lowercase and valid optional padding, not whitespace
    or otpauth URLs. Duplicate logins or malformed records fail closed.
    """
    if not _valid_login(login):
        raise AuthSecretsError("Invalid requested account login")
    try:
        raw = Path(secret_file).read_text(encoding="utf-8")
        document = json.loads(raw, object_pairs_hook=_unique_object)
    except AuthSecretsError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError):
        raise AuthSecretsError("Cannot read credentials JSON") from None
    if not isinstance(document, dict) or not isinstance(document.get("accounts"), list):
        raise AuthSecretsError("Invalid credentials schema")
    selected = None
    seen = set()
    for record in document["accounts"]:
        if not isinstance(record, dict) or not {"login", "password", "totp"} <= record.keys():
            raise AuthSecretsError("Missing or invalid account fields")
        credentials = Credentials(record["login"], record["password"], record["totp"])
        if credentials.login in seen:
            raise AuthSecretsError("Duplicate account login")
        seen.add(credentials.login)
        if credentials.login == login:
            selected = credentials
    if selected is None:
        raise AuthSecretsError("Requested account not found")
    return selected


def totp_code(secret: str, *, timestamp: int | float | None = None) -> tuple[str, float]:
    """Return (six-digit code, seconds left in its 30-second Unix-time window).

    Uses RFC 6238 / RFC 4226 dynamic truncation, HMAC-SHA1, and T0=0.
    The remaining lifetime is measured at the supplied/current timestamp.
    """
    key = _decode_totp(secret)
    current = time.time() if timestamp is None else timestamp
    if isinstance(current, bool) or not isinstance(current, (int, float)):
        raise AuthSecretsError("Invalid TOTP timestamp")
    try:
        current = float(current)
    except (ValueError, OverflowError):
        raise AuthSecretsError("Invalid TOTP timestamp") from None
    if not math.isfinite(current) or current < 0:
        raise AuthSecretsError("Invalid TOTP timestamp")
    counter = math.floor(current / 30)
    if counter >= 1 << 64:
        raise AuthSecretsError("Invalid TOTP timestamp")
    digest = hmac.digest(key, counter.to_bytes(8, "big"), hashlib.sha1)
    offset = digest[-1] & 15
    truncated = int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff
    return f"{truncated % 1_000_000:06d}", 30.0 - current % 30
