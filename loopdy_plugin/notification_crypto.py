"""Key, signature and encoding helpers for sealed notifications.

Sealed alerts and the notification grants use these to talk to the
notification service and to the phone.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


_B64URL = re.compile(r"^[A-Za-z0-9_-]+$")


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(value)).rstrip(b"=").decode("ascii")


def b64url_decode(value: Any, *, expected_length: int | None = None) -> bytes:
    if type(value) is not str:
        raise ValueError("Expected canonical unpadded base64url")
    normalized = value
    if not normalized or _B64URL.fullmatch(normalized) is None or len(normalized) % 4 == 1:
        raise ValueError("Expected canonical unpadded base64url")
    try:
        decoded = base64.b64decode(
            normalized + "=" * (-len(normalized) % 4),
            altchars=b"-_",
            validate=True,
        )
    except (ValueError, UnicodeEncodeError) as error:
        raise ValueError("Expected canonical unpadded base64url") from error
    if b64url_encode(decoded) != normalized:
        raise ValueError("Expected canonical unpadded base64url")
    if expected_length is not None and len(decoded) != expected_length:
        raise ValueError(f"Expected {expected_length} decoded bytes")
    return decoded


def public_key_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    if not isinstance(key.curve, ec.SECP256R1):
        raise ValueError("Notification keys must use P-256")
    return key.public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def public_key_from_x963(value: bytes) -> ec.EllipticCurvePublicKey:
    encoded = bytes(value)
    if len(encoded) != 65 or encoded[0] != 4:
        raise ValueError("P-256 public key must be a 65-byte uncompressed X9.63 point")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), encoded)
    except ValueError as error:
        raise ValueError("P-256 public key is not on the curve") from error


def key_id(public_key: bytes) -> str:
    public_key_from_x963(public_key)
    return b64url_encode(hashlib.sha256(public_key).digest())


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise ValueError("Value is not canonical JSON") from error


def sign_p1363(private_key: ec.EllipticCurvePrivateKey, value: bytes) -> bytes:
    if not isinstance(private_key.curve, ec.SECP256R1):
        raise ValueError("Notification signing key must use P-256")
    der = private_key.sign(bytes(value), ec.ECDSA(hashes.SHA256()))
    first, second = decode_dss_signature(der)
    return first.to_bytes(32, "big") + second.to_bytes(32, "big")

