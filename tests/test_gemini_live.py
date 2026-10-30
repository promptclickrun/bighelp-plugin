"""Gemini Live tokens: everything locked, the key stays here, fixed error codes."""
from datetime import datetime, timezone
import unittest
from unittest import mock

from loopdy_plugin import gemini_live
from loopdy_plugin.live_voice_provider import LiveProviderError

NOW = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.content = b"x" * 64

    def json(self):
        return self._body


class Client:
    def __init__(self, response):
        self.response = response
        self.requests = []

    async def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        return self.response


class GeminiLiveTests(unittest.IsolatedAsyncioTestCase):
    def test_setup_locks_model_voice_brief_tools_and_long_calls(self):
        setup = gemini_live.setup("You are Juniper.", "Charon")
        self.assertEqual(setup["model"], "models/gemini-3.8-live")
        self.assertEqual(setup["systemInstruction"], {"parts": [{"text": "You are Juniper."}]})
        voice = setup["generationConfig"]["speechConfig"]["voiceConfig"]["prebuiltVoiceConfig"]["voiceName"]
        self.assertEqual(voice, "Charon")
        self.assertEqual(setup["generationConfig"]["responseModalities"], ["AUDIO"])
        names = [f["name"] for tool in setup["tools"] for f in tool.get("functionDeclarations", [])]
        self.assertEqual(names, ["start_work", "check_work"])
        self.assertIn({"googleSearch": {}}, setup["tools"])
        self.assertIn("slidingWindow", setup["contextWindowCompression"])
        self.assertEqual(len(gemini_live.VOICES), 30)

    def test_an_unknown_voice_never_reaches_gemini(self):
        with self.assertRaises(LiveProviderError) as caught:
            gemini_live.setup("You are Juniper.", "NotAVoice")
        self.assertEqual(caught.exception.code, "unsupported_voice")

    async def test_mint_sends_one_use_locked_setup_and_returns_where_to_connect(self):
        client = Client(Response(200, {"name": "auth_tokens/abc_123"}))
        value = await gemini_live.mint("You are Juniper.", "Kore", key="host-key", http=client, now=NOW)
        url, kwargs = client.requests[0]
        self.assertEqual(url, "https://generativelanguage.googleapis.com/v1alpha/auth_tokens")
        self.assertEqual(kwargs["headers"], {"x-goog-api-key": "host-key"})
        body = kwargs["json"]
        self.assertEqual(body["uses"], 1)
        self.assertEqual(body["newSessionExpireTime"], "2026-10-30T12:01:00Z")
        self.assertEqual(body["expireTime"], "2026-10-30T12:30:00Z")
        self.assertNotIn("fieldMask", body)
        self.assertEqual(body["bidiGenerateContentSetup"], gemini_live.setup("You are Juniper.", "Kore"))
        self.assertEqual(value, {"transport": "websocket", "url": gemini_live.SOCKET_URL, "token": "auth_tokens/abc_123",
                                 "model": "models/gemini-3.8-live", "expiresAt": int(NOW.timestamp()) + 1800})
        self.assertNotIn("host-key", repr(value))

    async def test_failures_map_to_fixed_codes(self):
        for status, body, code in [(401, {}, "authentication_failed"), (403, {}, "access_denied"),
                                   (429, {}, "rate_limited"), (500, {}, "create_failed"),
                                   (200, {"name": "https://evil.example"}, "invalid_token"),
                                   (200, {}, "invalid_token")]:
            with self.assertRaises(LiveProviderError) as caught:
                await gemini_live.mint("You are Juniper.", "Kore", key="k", http=Client(Response(status, body)), now=NOW)
            self.assertEqual(caught.exception.code, code)
        client = Client(Response(200, {"name": "auth_tokens/x"}))
        with self.assertRaises(LiveProviderError) as caught:
            await gemini_live.mint("You are Juniper.", "Kore", key="", http=client, now=NOW)
        self.assertEqual(caught.exception.code, "credentials_unavailable")
        self.assertEqual(client.requests, [])

    def test_key_comes_from_the_agents_secret_scope(self):
        values = {"GOOGLE_API_KEY": " google-key "}
        with mock.patch("agent.secret_scope.get_secret", side_effect=values.get):
            self.assertEqual(gemini_live.api_key(), "google-key")
        values = {"GEMINI_API_KEY": "gemini-key", "GOOGLE_API_KEY": "google-key"}
        with mock.patch("agent.secret_scope.get_secret", side_effect=values.get):
            self.assertEqual(gemini_live.api_key(), "gemini-key")
        with mock.patch("agent.secret_scope.get_secret", return_value="   "):
            self.assertEqual(gemini_live.api_key(), "")


if __name__ == "__main__":
    unittest.main()
