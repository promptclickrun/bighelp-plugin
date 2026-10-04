from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from loopdy_plugin.workflow_contract import ContractError, check_rule, has_title, parse_outputs


OUTPUTS = [{"name": "draft", "type": "markdown_file"}, {"name": "word_count", "type": "number"},
           {"name": "summary", "type": "text"}, {"name": "decision", "type": "decision", "values": ["pass", "changes"]},
           {"name": "notes", "type": "notes"}]
GOOD = {"draft": {"content": "# Title\n\nBody text here."}, "word_count": 4, "summary": "Short.",
        "decision": "changes", "notes": [{"severity": "major", "text": "Fix the intro."}]}


def reply(outputs, fence="```"):
    return f"All done.\n\n{fence}bighelp-handoff\n{json.dumps({'outputs': outputs})}\n{fence}\n"


class HandoffTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()).resolve())
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        (self.dir / "out").mkdir()

    def parse(self, text, outputs=OUTPUTS):
        return parse_outputs(text, outputs, attempt_dir=self.dir, stage_title="Write the draft")

    def code(self, text, outputs=OUTPUTS):
        with self.assertRaises(ContractError) as caught:
            self.parse(text, outputs)
        return caught.exception.code

    def test_every_output_type_is_typed_and_stored_as_bytes(self):
        parsed = {output.name: output for output in self.parse(reply(GOOD))}
        self.assertEqual(parsed["draft"].data, b"# Title\n\nBody text here.")
        self.assertEqual(parsed["draft"].word_count, 5)
        self.assertEqual(parsed["word_count"].number_value, 4.0)
        self.assertEqual(parsed["summary"].value, "Short.")
        self.assertEqual(parsed["decision"].value, "changes")
        self.assertEqual(parsed["notes"].value, [{"severity": "major", "text": "Fix the intro."}])
        self.assertEqual(len(self.parse(reply({**GOOD, "extra": 1}, fence="~~~"))), 5)

    def test_block_count_and_json(self):
        self.assertEqual(self.code("No block at all."), "contract_no_block")
        self.assertEqual(self.code(reply(GOOD) + reply(GOOD)), "contract_many_blocks")
        self.assertEqual(self.code("```bighelp-handoff\n{not json}\n```"), "contract_bad_json")
        self.assertEqual(self.code('```bighelp-handoff\n{"outputs": 1}\n```'), "contract_bad_json")
        self.assertEqual(self.code('```bighelp-handoff\n{"outputs": {}, "outputs": {}}\n```'), "contract_bad_json")
        self.assertEqual(self.code('```bighelp-handoff\n{"outputs": {"word_count": NaN}}\n```'), "contract_bad_json")

    def test_missing_and_wrong_types(self):
        missing = dict(GOOD)
        missing.pop("word_count")
        self.assertEqual(self.code(reply(missing)), "contract_missing_output")
        for name, value in (("draft", "plain string"), ("draft", {"content": 1}), ("word_count", "12"),
                            ("word_count", True), ("decision", "maybe"), ("summary", 5),
                            ("notes", [{"severity": "huge", "text": "x"}]), ("notes", [{"text": "x"}]),
                            ("notes", [{"severity": "minor", "text": "x"}] * 21)):
            with self.subTest(name=name, value=value):
                self.assertEqual(self.code(reply({**GOOD, name: value})), "contract_wrong_type")

    def test_size_limits(self):
        self.assertEqual(self.code(reply({**GOOD, "summary": "x" * 8193})), "contract_too_large")
        self.assertEqual(self.code(reply({**GOOD, "draft": {"content": "x" * (512 * 1024 + 1)}})),
                         "contract_too_large")

    def test_files_come_only_from_the_out_folder(self):
        (self.dir / "out" / "draft.md").write_text("# From a file\n")
        parsed = self.parse(reply({**GOOD, "draft": {"path": "out/draft.md"}}))
        self.assertEqual(parsed[0].data, b"# From a file\n")
        secret = self.dir / "secret.txt"
        secret.write_text("not for you")
        (self.dir / "out" / "link.md").symlink_to(secret)
        os.link(secret, self.dir / "out" / "hard.md")
        (self.dir / "out" / "folder").mkdir()
        for path in ("brief.md", "out/../secret.txt", "/etc/hosts", "out/link.md", "out/missing.md",
                     "out/hard.md", "out/folder"):
            with self.subTest(path=path):
                self.assertEqual(self.code(reply({**GOOD, "draft": {"path": path}})), "contract_bad_path")
        (self.dir / "out" / "big.md").write_bytes(b"x" * (512 * 1024 + 1))
        self.assertEqual(self.code(reply({**GOOD, "draft": {"path": "out/big.md"}})), "contract_too_large")


class CheckRuleTests(unittest.TestCase):
    def test_rules(self):
        text = b"# Title\n\none two three"
        self.assertIsNone(check_rule({"type": "word_range", "of": "d.d", "min": 1, "max": 10}, "markdown_file", text))
        self.assertIn("5 words", check_rule({"type": "word_range", "of": "d.d", "min": 6, "max": 10},
                                            "markdown_file", text))
        self.assertIsNone(check_rule({"type": "has_title", "of": "d.d"}, "markdown_file", text))
        self.assertIsNotNone(check_rule({"type": "has_title", "of": "d.d"}, "markdown_file", b"no title\n# late"))
        self.assertIsNone(check_rule({"type": "not_empty", "of": "d.d"}, "text", b"x"))
        self.assertIsNotNone(check_rule({"type": "not_empty", "of": "d.d"}, "text", b"  \n"))
        self.assertIsNotNone(check_rule({"type": "not_empty", "of": "r.n"}, "notes", b"[]"))
        self.assertIsNone(check_rule({"type": "number_range", "of": "d.n", "min": 1, "max": 3}, "number", b"2"))
        self.assertIsNotNone(check_rule({"type": "number_range", "of": "d.n", "min": 1, "max": 3}, "number", b"7.5"))
        self.assertTrue(has_title("\n\n## Section"))
        self.assertFalse(has_title("#nospace"))


if __name__ == "__main__":
    unittest.main()
