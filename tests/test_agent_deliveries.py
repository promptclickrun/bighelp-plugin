"""`message_agent` messages as the agent asked keeps them (Hermes 0.23): receipts written in the shape of
Hermes' `runtime/bot_live_delivery/<id>.json`, the other agent's turn lease in its own state.db, and a real
child process as the gateway that holds it.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from loopdy_plugin import agent_deliveries, chat_live
from test_agents_activity import Agent, hermes_available, route_client
from test_native_api import PREFIX

CHAT = "20261108_183300_f679f6"
QUEUED, ANSWERED, WITHDRAWN, MISSING = "1" * 64, "2" * 64, "3" * 64, "4" * 64


@unittest.skipUnless(hermes_available() and agent_deliveries.available(), "this Hermes has no session store")
class AgentDeliveriesTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.ralph = Agent(Path(folder.name), "ralph")
        self.addCleanup(self.ralph.close)
        self.ralph.chat(CHAT, source="cli", title="Bot Chat")
        for name, value in (("profile_exists", lambda name: name == "ralph"),
                            ("get_profile_dir", lambda name: str(self.ralph.home))):
            patcher = patch(f"hermes_cli.profiles.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.receipt(QUEUED, "claimed")
        self.receipt(ANSWERED, "settled", reply="  Repo: promptclickrun/print-autocrop  ")
        self.receipt(WITHDRAWN, "cancelled")

    def receipt(self, delivery, status, reply=""):
        folder = self.ralph.home / "runtime" / "bot_live_delivery"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{delivery}.json").write_text(json.dumps({
            "admission_id": "a1", "author": {"id": "bot:default", "is_bot": True, "name": "hermes"},
            "delivery_id": delivery, "message": "Message from 🤖 Gordie (@hermes): Build it.", "operator": True,
            "principal_id": "uid:501", "profile_home": str(self.ralph.home), "reply": reply,
            "session_id": CHAT, "status": status}))

    def states(self, *deliveries):
        return {row["deliveryId"]: row for row in agent_deliveries.deliveries((("ralph", d) for d in deliveries))}

    def test_each_receipt_says_where_the_message_is_and_an_answer_says_what(self):
        rows = self.states(QUEUED, ANSWERED, WITHDRAWN, MISSING)
        self.assertEqual({key: row["state"] for key, row in rows.items()},
                         {QUEUED: "queued", ANSWERED: "answered", WITHDRAWN: "withdrawn"})
        self.assertEqual(rows[ANSWERED]["reply"], "Repo: promptclickrun/print-autocrop")
        self.assertNotIn("reply", rows[QUEUED])
        self.assertEqual(rows[QUEUED]["sessionId"], CHAT)
        self.assertNotIn("message", json.dumps(rows), "The message itself stays on the computer")

    @unittest.skipUnless(chat_live.available(), "this Hermes has no gateway owner forward")
    def test_a_message_the_gateway_is_working_on_says_so_with_its_tool(self):
        gateway = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (gateway.kill(), gateway.wait(timeout=5)))
        (self.ralph.home / "gateway.lock").write_text(json.dumps({"pid": gateway.pid, "kind": "hermes-gateway"}))
        self.ralph.say(CHAT, "user", "Message from 🤖 Gordie (@hermes): Build it.")
        self.ralph.turn(CHAT, pid=gateway.pid, platform="cli")
        self.ralph.say(CHAT, "assistant", tool=("call_1", "terminal"))
        row = self.states(QUEUED)[QUEUED]
        self.assertEqual((row["state"], row["tool"]), ("working", "terminal"))

    def test_an_agent_that_isnt_here_or_a_broken_receipt_is_left_out(self):
        (self.ralph.home / "runtime" / "bot_live_delivery" / f"{MISSING}.json").write_text("{not json")
        self.assertEqual(agent_deliveries.deliveries([("nova", ANSWERED), ("ralph", MISSING), ("ralph", "x")]), [])

    def test_the_route_answers_for_the_agents_asked(self):
        client = route_client((agent_deliveries.CAPABILITY,))
        self.addCleanup(client.stop)
        response = client.post(f"{PREFIX}/agents/deliveries", headers={
            "If-Match": client.etag, "X-Loopdy-Request-ID": "6f1d7e8a-3c2b-4a59-9e7d-2b1c0a9f8e7d"},
            json={"deliveries": [{"agentId": "ralph", "deliveryId": ANSWERED}]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["deliveries"][0]["state"], "answered")
        refused = client.post(f"{PREFIX}/agents/deliveries", headers={
            "If-Match": client.etag, "X-Loopdy-Request-ID": "6f1d7e8a-3c2b-4a59-9e7d-2b1c0a9f8e7e"},
            json={"deliveries": [{"agentId": "ralph", "deliveryId": "NOT-AN-ID"}]})
        self.assertEqual(refused.status_code, 422)


if __name__ == "__main__":
    unittest.main()
