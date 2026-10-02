from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from loopdy_plugin.managed_notifications import ManagedNotificationError
from loopdy_plugin.relay_crypto import b64url_decode, b64url_encode, canonical_json_bytes, key_id, public_key_bytes
from loopdy_plugin.sealed_alerts import MAX_ENVELOPE_BYTES, avatar_aad, seal_alert
from test_managed_notifications import ManagedNotificationTests, open_sealed

VECTOR = Path(__file__).resolve().parents[1] / "fixtures" / "contracts" / "sealed-alert-v2-vector.json"


def open_avatar(grant_id: str, avatar: dict, blob: bytes) -> bytes:
    assert hashlib.sha256(blob).hexdigest() == avatar["cipherSha256"]
    return AESGCM(b64url_decode(avatar["key"])).decrypt(b64url_decode(avatar["nonce"]), blob,
                                                        avatar_aad(grant_id, avatar["sha256"]))


class SealedAlertTests(ManagedNotificationTests):
    # The base setUp enrolls the phone and registers its content key (self.phone).

    def finish_turn(self, turn: str, text: str):
        self.service.observe("post_llm_call", profile="default", session_id="native-session", turn_id=turn,
                             assistant_response=text)
        self.service.observe("on_session_end", profile="default", session_id="native-session", turn_id=turn,
                             completed=True, platform="desktop")
        self.service.drain_pending()
        return self.calls[-1][2]

    def test_capabilities_advertise_sealed_alerts(self):
        self.assertEqual(self.service.capabilities()["sealedAlerts"], {"version": 2})

    def test_without_a_phone_key_no_alert_leaves_the_host(self):
        # Enrolled, but the phone hasn't registered its key yet: nothing is queued
        # or sent, rather than an alert the service could read.
        self.service.remove(self.grant_id)  # only the keyless enrollment below exists
        grant_id = str(uuid.uuid4())
        self.grant = dict(self.grant, grantId=grant_id)
        self.grant_id = grant_id
        self.service.enroll(grant_id, str(uuid.uuid4()))
        self.service.subscribe(grant_id, "default", "native-session", True)
        self.calls.clear()
        self.service.observe("post_llm_call", profile="default", session_id="native-session", turn_id="turn-a",
                             assistant_response="PRIVATE report text")
        self.service.observe("on_session_end", profile="default", session_id="native-session", turn_id="turn-a",
                             completed=True, platform="desktop")
        self.service.drain_pending()
        self.assertEqual([path for _, path, _, _ in self.calls if path.endswith("/events")], [])
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pending WHERE grant_id=? AND path='/events'",
                                        (grant_id,)).fetchone()[0], 0)
        # Once the key arrives, the next alert goes out sealed.
        self.service.register_recipient(grant_id, self.phone_public)
        body = json.loads(self.finish_turn("turn-b", "The report is ready."))
        self.assertEqual(body["version"], 3)
        self.assertEqual(open_sealed(body["sealed"], self.phone, b64url_decode(self.service.public_key))["body"],
                         "The report is ready.")

    def test_an_unsealed_alert_queued_before_the_update_is_never_sent(self):
        # A plugin older than this one could have queued an unsealed (version 2) alert.
        intent = self.grant_id + ":" + "c" * 64
        legacy = canonical_json_bytes({"version": 2, "eventId": intent, "eventType": "session.completed",
                                       "content": {"kind": "reply", "text": "PRIVATE reply"}, "sound": True})
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("INSERT INTO pending(intent_id,grant_id,path,raw,expires,state,next_attempt,session_ref)"
                       " VALUES(?,?,?,?,?,'pending',?,?)",
                       (intent, self.grant_id, "/events", legacy, self.now + 600, self.now, "legacy-ref"))
        self.service.drain_pending()
        self.assertEqual(self.calls, [])
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT state FROM pending WHERE intent_id=?", (intent,)).fetchone()[0],
                             "failed")

    def test_sealed_alert_hides_the_name_text_and_avatar_from_the_service(self):
        receipt = self.service.register_recipient(self.grant_id, self.phone_public)
        self.assertEqual(receipt["recipientKeyId"], key_id(public_key_bytes(self.phone.public_key())))
        raw = self.finish_turn("turn-a", "Your flight to Denver is booked for Friday.")
        for secret in (b"Fixture Agent", b"Denver", b"fixture-avatar", b"Zml4dHVyZS1hdmF0YXI"):
            self.assertNotIn(secret, raw)
        body = json.loads(raw)
        self.assertEqual(body["version"], 3)
        self.assertEqual(set(body), {"version", "eventId", "eventType", "sessionReference", "turnId", "occurredAt",
                                     "sealed", "avatar", "sound"})
        opened = open_sealed(body["sealed"], self.phone, b64url_decode(self.service.public_key))
        self.assertEqual((opened["title"], opened["body"]),
                         ("Fixture Agent", "Your flight to Denver is booked for Friday."))
        self.assertEqual((opened["eventId"], opened["eventType"]), (body["eventId"], body["eventType"]))
        blob = __import__("base64").b64decode(body["avatar"]["data"].split(",", 1)[1])
        self.assertEqual(hashlib.sha256(blob).hexdigest(), body["avatar"]["sha256"])
        self.assertEqual(open_avatar(self.grant_id, opened["avatar"], blob), b"fixture-avatar")

    def test_the_same_avatar_is_encrypted_identically(self):
        self.service.register_recipient(self.grant_id, self.phone_public)
        first = json.loads(self.finish_turn("turn-a", "First reply."))
        self.now += 60
        second = json.loads(self.finish_turn("turn-b", "Second reply."))
        self.assertEqual(first["avatar"], second["avatar"])
        self.assertNotEqual(first["sealed"]["ciphertext"], second["sealed"]["ciphertext"])

    def test_a_tampered_alert_fails_the_signature_check(self):
        self.service.register_recipient(self.grant_id, self.phone_public)
        envelope = json.loads(self.finish_turn("turn-a", "Done."))["sealed"]
        envelope["eventId"] = envelope["eventId"][:-1] + ("0" if envelope["eventId"][-1] != "0" else "1")
        with self.assertRaises(Exception):
            open_sealed(envelope, self.phone, b64url_decode(self.service.public_key))

    def test_long_text_still_fits_one_push(self):
        host = ec.generate_private_key(ec.SECP256R1())
        text = "Status update: " + "".join(chr(0x4E00 + (index * 7919) % 20000) for index in range(1_600))
        envelope = seal_alert(grant_id=self.grant_id, event_id=self.grant_id + ":" + "a" * 64,
                              event_type="session.completed", title="Fixture Agent", body=text,
                              avatar={"mimeType": "image/png", "sha256": "0" * 64, "cipherSha256": "1" * 64,
                                      "key": b64url_encode(b"k" * 32), "nonce": b64url_encode(b"n" * 12)},
                              recipient_public_key=public_key_bytes(self.phone.public_key()),
                              sender_private_key=host, issued=self.now)
        self.assertLessEqual(len(json.dumps(envelope, separators=(",", ":"))), MAX_ENVELOPE_BYTES)
        opened = open_sealed(envelope, self.phone, public_key_bytes(host.public_key()))
        self.assertTrue(text.startswith(opened["body"].rstrip("…")))
        # Ordinary replies keep every character.
        plain = "Here is the plan for tomorrow. " * 50
        full = open_sealed(seal_alert(grant_id=self.grant_id, event_id=self.grant_id + ":" + "b" * 64,
                                      event_type="session.completed", title="Fixture Agent", body=plain.strip(),
                                      avatar={"mimeType": "image/png", "sha256": "0" * 64, "cipherSha256": "1" * 64,
                                              "key": b64url_encode(b"k" * 32), "nonce": b64url_encode(b"n" * 12)},
                                      recipient_public_key=public_key_bytes(self.phone.public_key()),
                                      sender_private_key=host, issued=self.now),
                           self.phone, public_key_bytes(host.public_key()))
        self.assertEqual(full["body"], plain.strip())

    def test_invalid_or_foreign_keys_are_refused(self):
        with self.assertRaises(ManagedNotificationError):
            self.service.register_recipient(self.grant_id, "A" * 87)
        with self.assertRaises(ManagedNotificationError):
            self.service.register_recipient("00000000-0000-4000-8000-000000000000", self.phone_public)

    def test_revocation_removes_the_phone_key_and_avatar_keys(self):
        self.service.register_recipient(self.grant_id, self.phone_public)
        self.finish_turn("turn-a", "Done.")
        self.service.remove(self.grant_id)
        with sqlite3.connect(self.service.db_path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM avatar_keys").fetchone()[0], 0)

    def test_shared_vector_opens(self):
        vector = json.loads(VECTOR.read_text())
        phone = ec.derive_private_key(int.from_bytes(b64url_decode(vector["recipientPrivateKey"]), "big"), ec.SECP256R1())
        opened = open_sealed(vector["envelope"], phone, b64url_decode(vector["senderPublicKey"]))
        self.assertEqual(opened, vector["plaintext"])
        blob = b64url_decode(vector["avatarBlob"])
        self.assertEqual(open_avatar(vector["envelope"]["grantId"], opened["avatar"], blob),
                         b64url_decode(vector["avatarImage"]))


# The base class's own tests already run in test_managed_notifications.
for name in [name for name in dir(ManagedNotificationTests) if name.startswith("test_")]:
    setattr(SealedAlertTests, name, None)
