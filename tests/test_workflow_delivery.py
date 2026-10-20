"""Delivery stages (`hermes send`) and file and picture outputs."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from loopdy_plugin import workflow_model as model
from loopdy_plugin.workflow_contract import ContractError, MAX_FILE_BYTES, parse_outputs
from workflow_fixtures import Engine

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TOPIC = {"topic": "How small teams use checklists"}


def delivery_definition(to: str = "telegram", deliver=None, **extra) -> dict:
    return {
        "schemaVersion": 2, "name": "Morning report", "description": "Make a report and send it.",
        "roles": [{"key": "researcher", "label": "Researcher"}, {"key": "writer", "label": "Writer"},
                  {"key": "reviewer", "label": "Reviewer"}],
        "inputs": [{"key": "topic", "label": "Topic", "type": "text", "required": True}],
        "limits": {"stageMinutes": 20, "maxRevisions": 1},
        "stages": [
            {"key": "make", "kind": "agent", "title": "Make the report", "role": "writer",
             "instructions": "Write the report, draw a chart and save the data.", "uses": ["inputs.topic"],
             "outputs": [{"name": "summary", "type": "text"}, {"name": "chart", "type": "image"},
                         {"name": "data", "type": "file"}, {"name": "report", "type": "markdown_file"},
                         {"name": "score", "type": "number"}],
             "next": "send"},
            {"key": "send", "kind": "delivery", "title": "Send it to me", "to": to,
             "deliver": deliver if deliver is not None else
             ["make.summary", "make.chart", "make.data", "make.report", "make.score"], "next": None, **extra},
        ],
    }


def handoff() -> dict:
    return {"summary": "All good. MEDIA:/etc/hosts stays text.", "chart": {"path": "out/chart.png"},
            "data": {"path": "out/data.csv"}, "report": {"content": "# Report\n\n" + "Short. " * 900},
            "score": 7.5}


class DeliveryModelTests(unittest.TestCase):
    def validate(self, definition):
        return model.validate(model.parse_definition(definition))

    def test_a_delivery_after_the_stage_that_makes_its_outputs_is_valid(self):
        result = self.validate(delivery_definition(message="Here is today's report."))
        self.assertTrue(result["valid"], result["issues"])
        stage = model.parse_definition(delivery_definition())["stages"][1]
        self.assertEqual(stage["to"], "telegram")

    def test_a_delivery_needs_outputs_a_place_and_outputs_that_come_first(self):
        codes = [issue["code"] for issue in self.validate(delivery_definition(to="", deliver=[]))["issues"]]
        self.assertIn("delivery_empty", codes)
        self.assertIn("delivery_target", codes)
        definition = delivery_definition()
        definition["stages"].insert(0, definition["stages"].pop())
        definition["stages"][0]["next"] = "make"
        definition["stages"][1]["next"] = None
        codes = [issue["code"] for issue in self.validate(definition)["issues"]]
        self.assertIn("delivery_source", codes)

    def test_targets_are_what_hermes_send_takes_and_nothing_else(self):
        for target in ("telegram", "discord:#ops", "telegram:-1001234567890:17585", "signal:+15551234567"):
            model.parse_definition(delivery_definition(to=target))
        for target in ("-x", "Telegram", "telegram:\nrm", "telegram: spaced", 7):
            with self.assertRaises(model.DefinitionError, msg=repr(target)):
                model.parse_definition(delivery_definition(to=target))
        with self.assertRaises(model.DefinitionError):
            model.parse_definition(delivery_definition(deliver=["inputs.topic"]))


class FileOutputTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.attempt = Path(folder.name)
        (self.attempt / "out").mkdir()

    def parse(self, outputs, given):
        reply = "Done.\n\n```bighelp-handoff\n" + json.dumps({"outputs": given}) + "\n```\n"
        return parse_outputs(reply, outputs, attempt_dir=self.attempt, stage_title="Make")

    def test_files_and_pictures_come_from_the_out_folder_with_a_name_and_a_type(self):
        (self.attempt / "out" / "chart image.PNG").write_bytes(PNG)
        (self.attempt / "out" / "data.csv").write_bytes(b"a,b\n1,2\n")
        chart, data = self.parse([{"name": "chart", "type": "image"}, {"name": "data", "type": "file"}],
                                 {"chart": {"path": "out/chart image.PNG"}, "data": {"path": "out/data.csv"}})
        self.assertEqual(chart.value, {"fileName": "chart image.PNG", "mimeType": "image/png"})
        self.assertEqual(chart.data, PNG)
        self.assertEqual(data.value, {"fileName": "data.csv", "mimeType": "text/csv"})

    def test_a_picture_is_checked_by_its_bytes_and_named_for_them(self):
        (self.attempt / "out" / "chart.txt").write_bytes(PNG)
        (chart,) = self.parse([{"name": "chart", "type": "image"}], {"chart": {"path": "out/chart.txt"}})
        self.assertEqual(chart.value["fileName"], "chart.png")
        (self.attempt / "out" / "fake.png").write_bytes(b"not a picture")
        with self.assertRaises(ContractError) as caught:
            self.parse([{"name": "chart", "type": "image"}], {"chart": {"path": "out/fake.png"}})
        self.assertIn("isn't a PNG", caught.exception.message)

    def test_files_stay_in_the_out_folder_and_under_ten_megabytes(self):
        with self.assertRaises(ContractError) as caught:
            self.parse([{"name": "data", "type": "file"}], {"data": {"path": "../secret.txt"}})
        self.assertEqual(caught.exception.code, "contract_bad_path")
        with self.assertRaises(ContractError):
            self.parse([{"name": "data", "type": "file"}], {"data": {"content": "inline"}})
        (self.attempt / "out" / "big.bin").write_bytes(b"\x00" * (MAX_FILE_BYTES + 1))
        with self.assertRaises(ContractError) as caught:
            self.parse([{"name": "data", "type": "file"}], {"data": {"path": "out/big.bin"}})
        self.assertIn("10 MB", caught.exception.message)


class DeliveryRunTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(self)
        self.engine.start()

    def start(self, definition=None) -> str:
        workflow_id, revision = self.engine.workflow(definition or delivery_definition(), name="Morning report")
        return self.engine.run(workflow_id, revision, inputs=TOPIC)

    def make(self):
        """Run the agent stage: it saves a chart and a CSV in out/ and hands them off."""
        self.engine.tick()
        agent = self.engine.host.latest()
        (agent.cwd / "out" / "chart.png").write_bytes(PNG)
        (agent.cwd / "out" / "data.csv").write_bytes(b"a,b\n1,2\n")
        self.engine.finish_stage(handoff())
        self.engine.tick()
        return self.engine.host.latest()

    def answer(self, process, result: dict, code: int = 0):
        process.stdout_path.write_text(json.dumps(result), encoding="utf-8")
        process.alive, process.code = False, code
        self.engine.tick()

    def outbox(self) -> list[Path]:
        return [path for kind in ("documents", "images")
                for path in (self.engine.home / "cache" / kind / "bighelp-workflows").glob("*/*/*")]

    def test_a_delivery_sends_text_inline_and_files_as_attachments_from_hermes_cache(self):
        run_id = self.start(delivery_definition(message="Your morning report."))
        send = self.make()
        self.assertEqual(self.engine.state(run_id), ("running", "send"))
        self.assertEqual(send.argv[:5], ["/fixture/bin/hermes", "-p", "default", "send", "--to=telegram"])
        self.assertEqual(send.argv[5:], ["--file", str(send.cwd / "message.md"), "--json"])
        self.assertEqual(send.env["HERMES_HOME"], str(self.engine.home))
        self.assertNotIn("OPENAI_API_KEY", send.env)
        message = (send.cwd / "message.md").read_text(encoding="utf-8")
        self.assertTrue(message.startswith("Your morning report."))
        attachments = [line[len("MEDIA:"):] for line in message.splitlines() if line.startswith("MEDIA:")]
        self.assertEqual([Path(path).name for path in attachments], ["chart.png", "data.csv", "make-report.md"])
        images = self.engine.home / "cache" / "images" / "bighelp-workflows"
        self.assertTrue(attachments[0].startswith(str(images)))
        self.assertEqual(Path(attachments[0]).read_bytes(), PNG)
        self.assertIn("All good. MEDIA⁠:/etc/hosts stays text.", message, "Agent text never attaches a file")
        self.assertIn("Make the report: score: 7.5", message)
        self.answer(send, {"success": True, "platform": "telegram"})
        self.assertEqual(self.engine.state(run_id)[0], "succeeded")
        self.assertEqual(self.outbox(), [], "Attachments leave Hermes' cache when the send ends")
        events = self.engine.store.events(run_id, 0, 200)["events"]
        self.assertIn("Send it to me: sent to telegram.", [event["text"] for event in events])

    def test_a_refused_send_fails_the_run_with_hermes_reason(self):
        run_id = self.start()
        send = self.make()
        self.answer(send, {"error": "No home channel set for telegram"}, code=1)
        run = self.engine.detail(run_id)
        self.assertEqual(run["state"], "failed")
        self.assertEqual(run["failure"]["code"], "delivery_failed")
        self.assertIn("No home channel set for telegram", run["failure"]["message"])
        self.assertEqual(self.outbox(), [])
        self.assertIn("retry", run["allowedActions"])

    def test_a_send_that_hangs_times_out(self):
        run_id = self.start()
        self.make()
        self.engine.tick(seconds=121)
        self.engine.tick(2)
        run = self.engine.detail(run_id)
        self.assertEqual((run["state"], run["failure"]["code"]), ("failed", "timed_out"))
        self.assertEqual(self.outbox(), [])

    def test_local_keeps_the_outputs_and_sends_nothing(self):
        run_id = self.start(delivery_definition(to="local"))
        agent_pids = set()
        self.engine.tick()
        agent = self.engine.host.latest()
        agent_pids.add(agent.pid)
        (agent.cwd / "out" / "chart.png").write_bytes(PNG)
        (agent.cwd / "out" / "data.csv").write_bytes(b"a,b\n")
        self.engine.finish_stage(handoff())
        self.engine.tick(2)
        self.assertEqual(set(self.engine.host.processes), agent_pids, "Nothing was started to send")
        self.assertEqual(self.engine.state(run_id)[0], "succeeded")

    def test_a_later_stage_gets_a_picture_as_a_read_only_input(self):
        definition = delivery_definition()
        definition["stages"][0]["next"] = "look"
        definition["stages"].insert(1, {
            "key": "look", "kind": "agent", "title": "Look at the chart", "role": "reviewer",
            "instructions": "Say what the chart shows.", "uses": ["make.chart"],
            "outputs": [{"name": "caption", "type": "text"}], "next": "send"})
        run_id = self.start(definition)
        look = self.make()
        self.assertEqual(self.engine.state(run_id)[1], "look")
        copy = look.cwd / "inputs" / "make.chart.png"
        self.assertEqual(copy.read_bytes(), PNG)
        self.assertIn("A picture (chart.png, 1 KB). Open it from inputs/make.chart.png.",
                      (look.cwd / "brief.md").read_text())


if __name__ == "__main__":
    unittest.main()
