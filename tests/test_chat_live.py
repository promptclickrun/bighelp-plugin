"""A Bot Chat turn the agent's gateway runs (Hermes 0.23): the gateway's turn lease in Hermes' own state.db
says it runs, the watch follows it on a stand-in for the gateway's socket (Hermes' `owner_forward` client
API: `session.resume`, `session.events.since`, numbered `event` frames), and Stop interrupts it there.
A real child process stands in for the gateway, so its lease and lock name a live pid.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from loopdy_plugin import chat_activity, chat_live
from test_agents_activity import Agent, hermes_available

CHAT = "20261108_183300_f679f6"
EPOCH = "26a0fa671cf14baa85791da0e9d918e5"


def frame(seq, kind, **payload):
    return {"type": kind, "session_id": CHAT, "payload": payload, "seq": seq}


class FakeGateway:
    """The gateway's socket for one chat: `session.resume` says whether a turn runs, `session.events.since`
    replays what it holds, and `push` sends a live frame as the gateway would."""

    def __init__(self, running=True, replay=(), generation=7):
        self.running, self.replay, self.generation = running, list(replay), generation
        self.calls: list[tuple[str, dict]] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue | None = None
        self.ready = threading.Event()

    def push(self, *frames):
        self.ready.wait(5)
        for item in frames:
            self.loop.call_soon_threadsafe(self.queue.put_nowait, {"jsonrpc": "2.0", "method": "event", "params": item})

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == "session.resume":
            return {"session_id": CHAT, "running": self.running, "execution_generation": self.generation,
                    "replay_epoch": EPOCH, "last_sequence": max((f["seq"] for f in self.replay), default=0)}
        if method == "session.events.since":
            return {"events": self.replay, "replay_epoch": EPOCH, "snapshot_required": False}
        if method == "session.interrupt":
            self.running = False
            return {"ref": {}}
        raise AssertionError(method)

    @asynccontextmanager
    async def connect(self, home, endpoint, **_options):
        self.loop, self.queue = asyncio.get_running_loop(), asyncio.Queue()
        self.events = self.queue
        self.ready.set()
        yield self


@unittest.skipUnless(chat_live.available() and hermes_available(), "this Hermes has no gateway owner forward")
class ChatLiveTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.ralph = Agent(self.root, "ralph")
        self.addCleanup(self.ralph.close)
        self.ralph.chat(CHAT, source="cli", title="Bot Chat")
        self.ralph.say(CHAT, "user", "Message from 🤖 Gordie (@hermes): Build the app.\n\nReply needed: the repo")
        # The gateway: alive, and named by its lock as Hermes writes it.
        self.gateway = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: (self.gateway.kill(), self.gateway.wait(timeout=5)))
        (self.ralph.home / "gateway.lock").write_text(json.dumps(
            {"pid": self.gateway.pid, "kind": "hermes-gateway", "start_time": 1, "hermes_home": str(self.ralph.home)}))
        self.addCleanup(chat_live.forget)

    def use(self, gateway: FakeGateway):
        for name, value in (("discover_owner", lambda home: object()), ("connect_owner", gateway.connect)):
            patcher = patch(f"tui_gateway.owner_forward.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_the_gateways_turn_lease_is_a_turn_running_elsewhere(self):
        self.assertIsNone(chat_live.gateway_turn(self.ralph.home, CHAT, time.time()))
        holder = self.ralph.turn(CHAT, pid=self.gateway.pid, platform="cli")
        self.ralph.say(CHAT, "assistant", tool=("call_1", "terminal"))
        turn = chat_live.gateway_turn(self.ralph.home, CHAT, time.time())
        self.assertEqual(turn["tool"], "terminal")
        self.ralph.end_turn(CHAT, holder)
        self.assertIsNone(chat_live.gateway_turn(self.ralph.home, CHAT, time.time()))

        # This dashboard's own turn streams to the app already: not "elsewhere".
        holder = self.ralph.turn(CHAT, pid=os.getpid(), platform="cli")
        self.assertIsNone(chat_live.gateway_turn(self.ralph.home, CHAT, time.time()))
        self.ralph.end_turn(CHAT, holder)

    def test_chat_activity_says_a_gateway_turn_works_and_stop_reaches_it(self):
        gateway = FakeGateway()
        self.use(gateway)
        with patch.object(chat_activity, "_home", return_value=self.ralph.home):
            self.assertFalse(chat_activity.activity("ralph", CHAT)["working"])
            self.ralph.turn(CHAT, pid=self.gateway.pid, platform="cli")
            value = chat_activity.activity("ralph", CHAT)
            self.assertEqual((value["working"], value["canStop"], value["live"]), (True, True, True))
            self.assertTrue(chat_activity.stop("ralph", CHAT))
        self.assertIn(("session.interrupt", {"session_id": CHAT, "execution_generation": 7}), gateway.calls)

    def test_the_watch_streams_the_running_turn_from_its_start_then_only_new_frames(self):
        gateway = FakeGateway(replay=[
            frame(1, "message.start"), frame(2, "message.complete", text="An earlier turn."),
            frame(3, "message.start"), frame(4, "tool.start", tool_id="t1", name="terminal", context="sleep 7"),
            frame(5, "sessions.changed"),
        ])
        self.use(gateway)
        first = chat_live.follow(self.ralph.home, CHAT, None, None, 5)
        self.assertEqual([event["seq"] for event in first["events"]], [3, 4], "The running turn, from its start")
        self.assertEqual((first["running"], first["attached"], first["epoch"], first["last"]), (True, True, EPOCH, 4))

        gateway.push(frame(6, "tool.complete", tool_id="t1", name="terminal", result="x" * 20_000),
                     frame(7, "session.info", running=True, tools={"terminal": ["terminal"]}),
                     frame(8, "message.delta", text="Done", turn_id="u1", segment=0, offset=0))
        later = chat_live.follow(self.ralph.home, CHAT, 4, EPOCH, 5)
        while later["last"] < 8:
            more = chat_live.follow(self.ralph.home, CHAT, later["last"], EPOCH, 5)
            later = {**more, "events": later["events"] + more["events"]}
        self.assertEqual([event["type"] for event in later["events"]], ["tool.complete", "session.info", "message.delta"])
        self.assertEqual(len(later["events"][0]["payload"]["result"]), chat_live.MAX_TEXT, "Long output is cut")
        self.assertEqual(later["events"][1]["payload"], {"running": True}, "Only whether a turn runs")

        gateway.push(frame(9, "message.complete", text="Done"))
        done = chat_live.follow(self.ralph.home, CHAT, 8, EPOCH, 5)
        self.assertEqual((done["running"], done["last"]), (False, 9))

        # Numbering from another run of the gateway: the app starts the turn again.
        self.assertTrue(chat_live.follow(self.ralph.home, CHAT, 9, "another-epoch", 0)["reset"])

    def test_from_now_says_where_the_gateway_is_without_the_turn_so_far(self):
        """A chat that already shows what Hermes saved of the turn follows it from the gateway's newest frame,
        of any kind, so no step shows twice."""
        gateway = FakeGateway(replay=[frame(3, "message.start"), frame(4, "tool.start", tool_id="t1", name="terminal"),
                                      frame(5, "sessions.changed")])
        self.use(gateway)
        now = chat_live.follow(self.ralph.home, CHAT, None, None, 5, latest=True)
        self.assertEqual((now["events"], now["last"], now["running"]), ([], 5, True))
        gateway.push(frame(6, "tool.complete", tool_id="t1", name="terminal"))
        after = chat_live.follow(self.ralph.home, CHAT, now["last"], EPOCH, 5)
        self.assertEqual([event["seq"] for event in after["events"]], [6])

    def test_a_chat_with_nothing_running_has_no_frames_and_no_wait_past_its_limit(self):
        self.use(FakeGateway(running=False, replay=[frame(1, "message.start"), frame(2, "message.complete")]))
        started = time.monotonic()
        value = chat_live.follow(self.ralph.home, CHAT, None, None, 0.3)
        self.assertEqual((value["events"], value["running"]), ([], False))
        self.assertLess(time.monotonic() - started, 3)

    def test_without_a_gateway_the_app_hears_so_at_once(self):
        from tui_gateway.owner_forward import OwnerNotRunning

        def absent(home):
            raise OwnerNotRunning("the gateway that owns it is absent")
        with patch("tui_gateway.owner_forward.discover_owner", absent):
            value = chat_live.follow(self.ralph.home, CHAT, None, None, 2)
            self.assertFalse(value["attached"])
            # Hermes isn't asked again at once.
            self.assertEqual(chat_live.follow(self.ralph.home, CHAT, None, None, 2),
                             {"attached": False, "running": False, "events": [], "reset": False})


if __name__ == "__main__":
    unittest.main()
