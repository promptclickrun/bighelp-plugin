"""Key, signature and encoding helpers behind sealed notifications."""
from __future__ import annotations

import hashlib
import unittest

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from loopdy_plugin.notification_crypto import (
    b64url_decode,
    b64url_encode,
    canonical_json_bytes,
    key_id,
    public_key_bytes,
    public_key_from_x963,
    sign_p1363,
)


class NotificationCryptoTests(unittest.TestCase):
    def test_base64url_is_canonical_and_unpadded(self) -> None:
        self.assertEqual(b64url_encode(b"\xfb\xff"), "-_8")
        self.assertEqual(b64url_decode("-_8"), b"\xfb\xff")
        self.assertEqual(b64url_decode("AAAA", expected_length=3), b"\0\0\0")
        for invalid in ("", "AA==", "A+", "A/", "AB", "A\n"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                b64url_decode(invalid)
        for invalid in (True, 123, b"AA"):
            with self.subTest(invalid_type=type(invalid).__name__), self.assertRaises(ValueError):
                b64url_decode(invalid)
        with self.assertRaises(ValueError):
            b64url_decode("AAAA", expected_length=2)

    def test_public_keys_round_trip_and_name_their_hash(self) -> None:
        private = ec.derive_private_key(19, ec.SECP256R1())
        encoded = public_key_bytes(private.public_key())
        self.assertEqual(len(encoded), 65)
        self.assertEqual(public_key_bytes(public_key_from_x963(encoded)), encoded)
        self.assertEqual(key_id(encoded), b64url_encode(hashlib.sha256(encoded).digest()))
        for invalid in (encoded[:-1], b"\x02" + encoded[1:], b"\x04" + b"\0" * 64):
            with self.subTest(length=len(invalid)), self.assertRaises(ValueError):
                public_key_from_x963(invalid)
        with self.assertRaisesRegex(ValueError, "P-256"):
            public_key_bytes(ec.generate_private_key(ec.SECP384R1()).public_key())

    def test_canonical_json_is_sorted_compact_and_finite(self) -> None:
        self.assertEqual(canonical_json_bytes({"b": 1, "a": "é"}), '{"a":"é","b":1}'.encode())
        with self.assertRaises(ValueError):
            canonical_json_bytes({"value": float("nan")})

    def test_signatures_are_raw_p1363_that_verify(self) -> None:
        private = ec.derive_private_key(23, ec.SECP256R1())
        signature = sign_p1363(private, b"signed bytes")
        self.assertEqual(len(signature), 64)
        der = encode_dss_signature(int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big"))
        private.public_key().verify(der, b"signed bytes", ec.ECDSA(hashes.SHA256()))
        with self.assertRaisesRegex(ValueError, "P-256"):
            sign_p1363(ec.generate_private_key(ec.SECP384R1()), b"signed bytes")


if __name__ == "__main__":
    unittest.main()
