from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import struct
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from loopdy_plugin.managed_notifications import ManagedNotifications
from loopdy_plugin.notification_crypto import b64url_decode
from loopdy_plugin.sealed_alerts import avatar_aad

if __package__:
    from . import test_managed_notifications as fixtures
else:
    import test_managed_notifications as fixtures

# Captured before any test replaces it with a fixed picture.
REAL_PRESENTATION = ManagedNotifications.__dict__["_agent_presentation"]
DEFAULT_PICTURE = Path(__file__).resolve().parents[1] / "loopdy_plugin" / "assets" / "default-agent-avatar.png"
EVERY_EVENT_TYPE = ["session.completed", "session.failed", "scheduled.completed", "scheduled.failed",
                    "approval.required", "clarification.required", "subagent.completed", "subagent.failed"]
# The notification service takes an alert's encrypted picture up to this many characters.
SERVICE_AVATAR_DATA_LIMIT = 700_000
LOGGER = "hermes.plugins.bighelp.notifications"


class AgentPictureNotificationTests(unittest.TestCase):
    """Every alert carries a picture: the agent's own, or bighelp's when the agent has none it can use.

    The notification service refuses an alert without a picture. The app saves one only when
    someone picks it, so the default agent on a new host, or an agent made with the Hermes CLI,
    used to get no alerts at all.
    """

    service: ManagedNotifications
    grant: dict[str, Any]
    grant_id: str
    calls: list[tuple[str, str, bytes, dict[str, str]]]
    now: int
    temp: tempfile.TemporaryDirectory
    tearDown = fixtures.ManagedNotificationTests.tearDown
    get_session = fixtures.ManagedNotificationTests.get_session
    transport = fixtures.ManagedNotificationTests.transport
    workflow_journal = fixtures.ManagedNotificationTests.workflow_journal

    def setUp(self):
        fixtures.ManagedNotificationTests.setUp(self)
        # The real lookup, against a Hermes that answers profiles.get_asset the way 0.22.2 does.
        real = patch.object(ManagedNotifications, "_agent_presentation", REAL_PRESENTATION)
        real.start()
        self.addCleanup(real.stop)
        server = ModuleType("tui_gateway.server")
        server.handle_request = self.hermes  # type: ignore[attr-defined]
        package = ModuleType("tui_gateway")
        package.__path__ = []  # type: ignore[attr-defined]
        package.server = server  # type: ignore[attr-defined]
        modules = patch.dict("sys.modules", {"tui_gateway": package, "tui_gateway.server": server})
        modules.start()
        self.addCleanup(modules.stop)
        names = patch("loopdy_plugin.adapter.profile_display_name",
                      side_effect={"default": "Fixture Agent", "nova": "Nova"}.get)
        names.start()
        self.addCleanup(names.stop)
        self.profiles = Path(self.temp.name) / "profiles"
        for profile in ("default", "nova"):
            (self.profiles / profile).mkdir(parents=True)
        self.hermes_raises = False
        self.lookups: list[str] = []

    def hermes(self, request: dict[str, Any]) -> dict[str, Any]:
        """profiles.get_asset as Hermes 0.22.2 answers it (tui_gateway/methods_profiles.py):
        no picture is ``found: false``, not an error, and Hermes has no default picture."""
        self.assertEqual(request["method"], "profiles.get_asset")
        rid, params = request["id"], request["params"]
        self.lookups.append(params["name"])
        if self.hermes_raises:
            raise OSError("synthetic read failure")
        folder = self.profiles / params["name"]
        if not folder.is_dir():
            return {"jsonrpc": "2.0", "id": rid,
                    "error": {"code": 4064, "message": f"profile '{params['name']}' not found"}}
        for extension, mime in (("png", "image/png"), ("jpg", "image/jpeg"), ("webp", "image/webp")):
            target = folder / "assets" / f"{params['asset']}.{extension}"
            if target.is_file():
                blob = target.read_bytes()
                return {"jsonrpc": "2.0", "id": rid, "result": {
                    "found": True, "mime": mime, "size": len(blob),
                    "data": f"data:{mime};base64,{base64.b64encode(blob).decode('ascii')}"}}
        return {"jsonrpc": "2.0", "id": rid, "result": {"found": False}}

    def save_picture(self, profile: str, blob: bytes, extension: str = "png"):
        """What profiles.set_asset leaves on disk (it takes up to 2 MB)."""
        folder = self.profiles / profile / "assets"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"avatar.{extension}").write_bytes(blob)

    def allow_every_kind(self):
        grant = dict(self.grant, eventTypes=EVERY_EVENT_TYPE)
        with sqlite3.connect(self.service.db_path) as db:
            db.execute("UPDATE grants SET public_json=? WHERE grant_id=?",
                       (json.dumps(grant, separators=(",", ":"), sort_keys=True), self.grant_id))

    def reply(self, turn: str = "turn-a", text: str = "Here is your answer."):
        self.service.observe("post_llm_call", profile="default", session_id="native-session", turn_id=turn,
                             assistant_response=text, platform="desktop")

    def alerts(self) -> list[dict[str, Any]]:
        """Every alert queued for the phone, opened the way the phone opens it, picture included."""
        opened = []
        with sqlite3.connect(self.service.db_path) as db:
            rows = db.execute("SELECT p.raw, e.detail_json FROM pending p JOIN events e ON e.event_id=p.intent_id "
                              "WHERE p.path='/events' ORDER BY p.rowid").fetchall()
        for raw, detail_json in rows:
            body = json.loads(raw)
            detail = json.loads(detail_json)
            sealed = fixtures.open_sealed(body["sealed"], self.phone, b64url_decode(self.service.public_key))
            data = body["avatar"]["data"]
            self.assertTrue(data.startswith("data:application/octet-stream;base64,"))
            self.assertLessEqual(len(data), SERVICE_AVATAR_DATA_LIMIT)
            blob = base64.b64decode(data.split(",", 1)[1])
            self.assertEqual(hashlib.sha256(blob).hexdigest(), body["avatar"]["sha256"])
            picture = sealed["avatar"]
            image = AESGCM(b64url_decode(picture["key"])).decrypt(
                b64url_decode(picture["nonce"]), blob, avatar_aad(self.grant_id, picture["sha256"]))
            self.assertEqual(hashlib.sha256(image).hexdigest(), picture["sha256"])
            opened.append({"eventType": body["eventType"], "kind": detail["content"]["kind"],
                           "title": sealed["title"], "text": sealed["body"], "mimeType": picture["mimeType"],
                           "sha256": picture["sha256"], "image": image,
                           "detailSha256": detail["agent"]["avatarSha256"]})
        return opened

    def assert_default_picture(self, alert: dict[str, Any]):
        default = DEFAULT_PICTURE.read_bytes()
        self.assertEqual((alert["mimeType"], alert["sha256"], alert["detailSha256"]),
                         ("image/png", hashlib.sha256(default).hexdigest(), hashlib.sha256(default).hexdigest()))
        self.assertEqual(alert["image"], default)

    def test_an_agent_without_a_picture_alerts_with_bighelps_picture(self):
        with self.assertLogs(LOGGER, "WARNING") as logs:
            self.reply("turn-a", "Here is your answer.")
            self.reply("turn-b", "Here is the next one.")
        alerts = self.alerts()
        self.assertEqual([(a["eventType"], a["title"], a["text"]) for a in alerts], [
            ("session.completed", "Fixture Agent", "Here is your answer."),
            ("session.completed", "Fixture Agent", "Here is the next one.")])
        for alert in alerts:
            self.assert_default_picture(alert)
        # One line per agent and reason, with no names or paths in it.
        self.assertEqual(len(logs.records), 1, logs.output)
        line = logs.records[0].getMessage()
        self.assertIn("default agent picture", line)
        self.assertNotIn("Fixture Agent", line)
        self.assertNotIn(self.temp.name, line)
        self.service.drain_pending()
        self.assertEqual(len(fixtures.ManagedNotificationTests.events_sent(self)), 2, "Both alerts leave the host")

    def test_every_kind_of_alert_falls_back_to_bighelps_picture(self):
        journal = self.workflow_journal()
        self.service.queue_workflow_alerts()  # The first look only marks where the journal starts.
        self.allow_every_kind()
        self.service.set_preferences(self.grant_id, peer_chats=True)
        self.reply("turn-reply", "Here is your answer.")
        self.service.observe("on_session_end", profile="default", session_id="native-session",
                             turn_id="turn-failed", failed=True, error="The provider stopped answering.",
                             platform="desktop")
        self.service.observe("post_llm_call", profile="default", session_id="native-session",
                             turn_id="turn-scheduled", assistant_response="Your morning summary.", platform="cron")
        self.service.observe("pre_tool_call", profile="default", session_id="native-session",
                             turn_id="turn-question", tool_call_id="toolu_01question", tool_name="clarify",
                             args={"question": "Which city should I book?"})
        self.service.observe("pre_approval_request", profile="default", surface="gateway", coalesced=False,
                             session_id="native-session", turn_id="turn-approval",
                             tool_call_id="toolu_01approval", description="Delete the old drafts")
        self.service.observe("pre_llm_call", profile="default", session_id="native-session",
                             turn_id="turn-helper", platform="desktop")
        self.service.observe("subagent_start", profile="default", parent_session_id="native-session",
                             parent_turn_id="turn-helper", child_session_id="child-1", child_goal="Check the build")
        self.service.observe("subagent_stop", profile="default", parent_session_id="native-session",
                             parent_turn_id="turn-helper", child_session_id="child-1", child_status="completed")
        journal.execute("INSERT INTO events (run_id, at, kind, text) VALUES ('run_1', 2, 'succeeded', 'The run is done.')")
        journal.commit()
        journal.close()
        self.service.queue_workflow_alerts()
        with patch.object(ManagedNotifications, "_local_agent", side_effect={"nova": "nova"}.get):
            self.service.observe("pre_llm_call", profile="default", session_id="native-session",
                                 turn_id="turn-message", platform="desktop",
                                 user_message="Message from 🤖 Nova (@nova): The numbers are in the sheet.")
        alerts = self.alerts()
        self.assertEqual(sorted((a["kind"], a["eventType"]) for a in alerts), [
            ("approval", "approval.required"),
            ("clarification", "clarification.required"),
            ("failure", "session.failed"),
            ("reply", "session.completed"),
            ("reply", "session.completed"),
            ("scheduled", "scheduled.completed"),
            ("subagent", "subagent.completed"),
            ("workflow", "session.completed"),
        ])
        for alert in alerts:
            self.assert_default_picture(alert)
        # Another agent's message reads as from that agent, with bighelp's picture, not the other agent's.
        self.assertIn(("Nova", "The numbers are in the sheet."), [(a["title"], a["text"]) for a in alerts])

    def test_a_picture_the_notification_service_cannot_take_falls_back(self):
        png = b"\x89PNG\r\n\x1a\n"
        cases = {
            # Hermes stores pictures up to 2 MB; alerts take 512 KB.
            "too large": lambda: self.save_picture("default", png + b"\0" * 600_000),
            "empty file": lambda: self.save_picture("default", b""),
            "agent folder gone": lambda: (self.profiles / "default").rename(self.profiles / "default-old"),
            "Hermes fails to read it": lambda: setattr(self, "hermes_raises", True),
        }
        for index, (name, arrange) in enumerate(cases.items()):
            with self.subTest(name):
                self.hermes_raises = False
                arrange()
                self.reply(f"turn-{index}", f"Answer {index}.")
                alerts = [a for a in self.alerts() if a["text"] == f"Answer {index}."]
                self.assertEqual(len(alerts), 1, "The alert still goes out")
                self.assert_default_picture(alerts[0])
                if (self.profiles / "default-old").exists():
                    (self.profiles / "default-old").rename(self.profiles / "default")
                for picture in (self.profiles / "default" / "assets").glob("avatar.*"):
                    picture.unlink()

    def test_an_agent_with_its_own_picture_keeps_it(self):
        own = b"\x89PNG\r\n\x1a\n" + b"fixture-agent-picture"
        self.save_picture("default", own)
        with self.assertNoLogs(LOGGER, "WARNING"):
            self.reply()
        (alert,) = self.alerts()
        self.assertEqual((alert["mimeType"], alert["sha256"], alert["image"]),
                         ("image/png", hashlib.sha256(own).hexdigest(), own))
        self.assertEqual(self.lookups, ["default"])

    def test_the_default_picture_ships_in_the_package_and_fits_an_alert(self):
        blob = DEFAULT_PICTURE.read_bytes()
        self.assertEqual(blob[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", blob[16:24]), (256, 256))
        self.assertLessEqual(len(blob), 64 * 1024, "Small enough to send often")
        # Loaded from the package folder, wherever Hermes runs from.
        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        cwd = os.getcwd()
        os.chdir(elsewhere.name)
        self.addCleanup(os.chdir, cwd)
        name, picture = self.service._agent_presentation("default")
        self.assertEqual(name, "Fixture Agent")
        self.assertEqual(picture, {"mimeType": "image/png", "sha256": hashlib.sha256(blob).hexdigest(),
                                   "data": "data:image/png;base64," + base64.b64encode(blob).decode("ascii")})


if __name__ == "__main__":
    unittest.main()
