"""End-to-end sealed notification content (managed alerts, version 2).

Only the recipient phone can read an alert's title, text and avatar. The phone
gives its public key to this host directly (never through the notification
service), and the host signs every alert with the notification key the phone
pinned from this host. The notification service and BuzzKit forward opaque bytes.

Format (all binary fields unpadded base64url):
- AAD: "loopdy-sealed-alert-v2", grantId, eventId, recipientKeyId, senderKeyId, issued (newline-joined)
- key: HKDF-SHA256(ECDH(ephemeral, recipient), salt, "loopdy-sealed-alert-key-v2\\0" + SHA256(AAD))
- content: AES-256-GCM(raw-DEFLATE(canonical JSON plaintext), AAD)
- signature: ECDSA P-256/SHA-256 (r||s) by the host key over the signature input below
- avatar: AES-256-GCM(image) under a per-grant, per-image key carried inside the plaintext
"""
from __future__ import annotations

import hashlib
import os
import zlib
from collections.abc import Callable
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .relay_crypto import (
    b64url_encode,
    canonical_json_bytes,
    key_id,
    public_key_bytes,
    public_key_from_x963,
    sign_p1363,
)

VERSION = 2
# Leaves room for BuzzKit's routing fields inside the 4 KB push payload.
MAX_ENVELOPE_BYTES = 3_000
_AAD_LABEL = "loopdy-sealed-alert-v2"
_KEY_LABEL = b"loopdy-sealed-alert-key-v2\0"
_SIGNATURE_LABEL = b"loopdy-sealed-alert-signature-v2"
_AVATAR_LABEL = "loopdy-sealed-avatar-v2"


def alert_aad(*, grant_id: str, event_id: str, recipient_key_id: str, sender_key_id: str, issued: int) -> bytes:
    fields = [_AAD_LABEL, grant_id, event_id, recipient_key_id, sender_key_id, str(int(issued))]
    if any(not field or "\n" in field for field in fields):
        raise ValueError("Sealed alert fields must be non-empty single-line values")
    return "\n".join(fields).encode("utf-8")


def signature_input(*, aad: bytes, ephemeral_public_key: bytes, salt: bytes, nonce: bytes,
                    ciphertext: bytes, tag: bytes) -> bytes:
    parts = [_SIGNATURE_LABEL, hashlib.sha256(aad).hexdigest().encode("ascii")]
    parts += [b64url_encode(value).encode("ascii") for value in (ephemeral_public_key, salt, nonce, ciphertext, tag)]
    return b"\n".join(parts)


def avatar_aad(grant_id: str, image_sha256: str) -> bytes:
    return "\n".join([_AVATAR_LABEL, grant_id, image_sha256]).encode("utf-8")


def seal_avatar(*, grant_id: str, image: bytes, key: bytes, nonce: bytes) -> bytes:
    """Encrypted avatar bytes (ciphertext followed by the 16-byte tag)."""
    if len(key) != 32 or len(nonce) != 12:
        raise ValueError("Avatar key and nonce must be 32 and 12 bytes")
    return AESGCM(key).encrypt(nonce, image, avatar_aad(grant_id, hashlib.sha256(image).hexdigest()))


def _deflate(value: bytes) -> bytes:
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)  # raw DEFLATE, as Apple's COMPRESSION_ZLIB reads it
    return compressor.compress(value) + compressor.flush()


def seal_alert(*, grant_id: str, event_id: str, event_type: str, title: str, body: str,
               avatar: dict[str, Any], recipient_public_key: bytes,
               sender_private_key: ec.EllipticCurvePrivateKey, issued: int,
               random: Callable[[int], bytes] = os.urandom,
               ephemeral: Callable[[], ec.EllipticCurvePrivateKey] | None = None) -> dict[str, Any]:
    """The sealed envelope for one alert. Text that would not fit one push is shortened."""
    recipient = public_key_from_x963(recipient_public_key)
    recipient_id = key_id(recipient_public_key)
    sender_id = key_id(public_key_bytes(sender_private_key.public_key()))
    aad = alert_aad(grant_id=grant_id, event_id=event_id, recipient_key_id=recipient_id,
                    sender_key_id=sender_id, issued=issued)
    make_ephemeral = ephemeral or (lambda: ec.generate_private_key(ec.SECP256R1()))
    text = body
    while True:
        plaintext = canonical_json_bytes({"v": VERSION, "eventId": event_id, "eventType": event_type,
                                          "title": title, "body": text, "avatar": avatar})
        ephemeral_key = make_ephemeral()
        ephemeral_public = public_key_bytes(ephemeral_key.public_key())
        salt, nonce = random(32), random(12)
        shared = ephemeral_key.exchange(ec.ECDH(), recipient)
        derived = HKDF(algorithm=hashes.SHA256(), length=32, salt=salt,
                       info=_KEY_LABEL + hashlib.sha256(aad).digest()).derive(shared)
        combined = AESGCM(derived).encrypt(nonce, _deflate(plaintext), aad)
        ciphertext, tag = combined[:-16], combined[-16:]
        signed = signature_input(aad=aad, ephemeral_public_key=ephemeral_public, salt=salt,
                                 nonce=nonce, ciphertext=ciphertext, tag=tag)
        envelope = {
            "v": VERSION,
            "grantId": grant_id,
            "eventId": event_id,
            "recipientKeyId": recipient_id,
            "senderKeyId": sender_id,
            "issued": int(issued),
            "ephemeralPublicKey": b64url_encode(ephemeral_public),
            "salt": b64url_encode(salt),
            "nonce": b64url_encode(nonce),
            "ciphertext": b64url_encode(ciphertext),
            "tag": b64url_encode(tag),
            "signature": b64url_encode(sign_p1363(sender_private_key, signed)),
        }
        if len(canonical_json_bytes(envelope)) <= MAX_ENVELOPE_BYTES:
            return envelope
        if len(text) <= 1:
            raise ValueError("Sealed alert does not fit one push")
        text = text[: max(1, int(len(text) * 0.85)) - 1].rstrip() + "…"


__all__ = ["MAX_ENVELOPE_BYTES", "VERSION", "alert_aad", "avatar_aad", "seal_alert", "seal_avatar", "signature_input"]
