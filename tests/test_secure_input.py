"""Agent-requested secure input via Hermes' secret-capture pathway."""
import json
import unittest

from loopdy_plugin import secure_input


class SecureInputTests(unittest.TestCase):
    def call(self, args, *, callback=None, interactive=True, saved=False, provider=False):
        self.allowed = []
        self.asked = []

        def capture(name, prompt, metadata):
            self.asked.append((name, prompt, metadata))
            return callback(name) if callback else {"success": True, "stored_as": name, "skipped": False}

        raw = secure_input.request(
            args, callback=capture, interactive=lambda: interactive, saved=lambda _: saved,
            allow=self.allowed.append, provider_credential=lambda _: provider)
        self.assertNotIn("hunter2", raw)
        return json.loads(raw)

    def test_saves_through_the_pop_up_and_never_returns_the_value(self):
        result = self.call({"name": "BANK_PASSWORD", "prompt": "Enter your  bank\npassword.", "label": "Bank password"},
                           callback=lambda name: {"success": True, "stored_as": name, "skipped": False, "value": "hunter2"})
        self.assertEqual(result["success"], True)
        self.assertEqual(result["stored_as"], "BANK_PASSWORD")
        self.assertEqual(self.asked, [("BANK_PASSWORD", "Enter your bank password.",
                                       {"source": "agent", "label": "Bank password"})])
        self.assertEqual(self.allowed, ["BANK_PASSWORD"])

    def test_cancelled_pop_up_is_reported_and_nothing_is_allowed(self):
        result = self.call({"name": "GITHUB_TOKEN", "prompt": "Paste a token."},
                           callback=lambda name: {"success": True, "stored_as": name, "skipped": True})
        self.assertEqual((result["success"], result["skipped"]), (False, True))
        self.assertEqual(self.allowed, [])

    def test_saved_value_is_reused_unless_replaced(self):
        result = self.call({"name": "GITHUB_TOKEN", "prompt": "Paste a token."}, saved=True)
        self.assertEqual(result["already_saved"], True)
        self.assertEqual(self.asked, [])
        self.call({"name": "GITHUB_TOKEN", "prompt": "Paste a new token.", "replace": True}, saved=True)
        self.assertEqual(len(self.asked), 1)

    def test_messaging_chats_cannot_show_the_pop_up(self):
        result = self.call({"name": "GITHUB_TOKEN", "prompt": "Paste a token."}, interactive=False)
        self.assertEqual(result["error"], "secure_input_unavailable")
        self.assertEqual(self.asked, [])

    def test_system_names_and_provider_keys_are_refused(self):
        for name in ["PATH", "HERMES_HOME", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "PYTHONPATH", "lower_case", "X",
                     "A" * 65, 42, None]:
            self.assertEqual(self.call({"name": name, "prompt": "x"})["error"], "invalid_name", name)
        self.assertEqual(self.call({"name": "OPENAI_API_KEY", "prompt": "x"}, provider=True)["error"],
                         "provider_credential")
        self.assertEqual(self.call({"name": "GITHUB_TOKEN", "prompt": "  "})["error"], "missing_prompt")
        self.assertEqual(self.asked, [])

    def test_a_failing_pop_up_is_an_error_not_a_crash(self):
        def broken(name):
            raise RuntimeError("socket closed")
        self.assertEqual(self.call({"name": "GITHUB_TOKEN", "prompt": "x"}, callback=broken)["error"],
                         "secure_input_failed")


if __name__ == "__main__":
    unittest.main()
