"""Live voice reads the selected agent's own Codex sign-in (#14)."""
import contextlib
import contextvars
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import httpx

from loopdy_plugin.live_voice_auth import CodexLiveAuth, LiveAuthError, LiveCredentialScopeError
from loopdy_plugin.live_voice_provider import CodexLiveProvider, LiveProviderError
from loopdy_plugin.native_context import NativeAPIError, NativeContext
from loopdy_plugin.native_voice import NativeVoiceHub

SDP = "v=0\r\nm=audio 9 UDP/TLS/RTP/SAVPF 111\r\n"
_profile = contextvars.ContextVar("profile", default=None)


class UnscopedSecretError(RuntimeError):
    """Named like Hermes' error for a credential read outside any profile scope."""


class ProfileScopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_setup_reads_credentials_in_the_agents_profile(self):
        # Hosts serving several profiles refuse credential reads outside one;
        # the refusal read as "sign in again".
        seen = []

        class Provider:
            def __init__(self, **_kwargs):
                pass

            async def create(self, sdp, **_kwargs):
                seen.append(_profile.get())
                return SDP

            async def close(self):
                pass

        @contextlib.contextmanager
        def scope(profile):
            token = _profile.set(profile)
            try:
                yield None
            finally:
                _profile.reset(token)

        hub = NativeVoiceHub(provider_factory=lambda **kwargs: Provider(**kwargs), lease_seconds=30)
        owner = NativeContext("fixture", "alice", None, "research", ("native-voice-v1",), "runtime")
        fields = {"agentId": "research", "sessionId": "stored", "voiceId": "voice_fixture",
                  "provider": "codex_subscription", "voice": "cove", "sdp": SDP}
        profiles = SimpleNamespace(_config_profile_scope=scope)
        try:
            with patch.dict(sys.modules, {"hermes_cli.web_server_profiles": profiles}):
                self.assertEqual((await hub.offer(owner, fields))["sdp"], SDP)
        finally:
            await hub.shutdown()
        self.assertEqual(seen, ["research"])
        self.assertIsNone(_profile.get())

    async def test_a_credential_scope_failure_is_not_called_a_sign_in_failure(self):
        def unscoped(**_kwargs):
            raise UnscopedSecretError("read outside a profile scope")

        with self.assertRaises(LiveCredentialScopeError):
            await CodexLiveAuth(unscoped).resolve()

        def rejected(**_kwargs):
            raise PermissionError("signed out")

        with self.assertRaises(LiveAuthError) as plain:
            await CodexLiveAuth(rejected).resolve()
        self.assertNotIsInstance(plain.exception, LiveCredentialScopeError)

        async def handler(_request):
            raise AssertionError("no voice request without credentials")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = CodexLiveProvider(auth=CodexLiveAuth(unscoped), http_client=client)
            with self.assertRaises(LiveProviderError) as error:
                await provider.create(SDP)
        self.assertEqual(error.exception.code, "credentials_unavailable")


if __name__ == "__main__":
    unittest.main()
