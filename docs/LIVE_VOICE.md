# Live voice

Live voice lets you talk with an agent in real time from the bighelp app. The plugin only sets up the audio
call and relays its events. The actual work runs as an ordinary Hermes chat, so Hermes stays in charge of
tools, approvals and history.

## How a call works

1. The app checks `status` for the chosen provider.
2. The app sends its WebRTC `offer`. The host connects to the voice provider with its own credentials and
   returns the provider's answer. Audio then flows directly between the phone and the provider.
3. The app `poll`s the call's events: started, live captions, and requests from the voice model to hand work
   to the agent.
4. For each hand-off, the app sends the request as a normal message in the agent's Hermes chat. When the reply
   arrives, it posts a short `result` (up to 1,500 characters) so the voice model can speak it.
5. `close` ends the call. Ending or muting a call doesn't cancel work already handed to the agent.

All five operations are `POST /api/plugins/loopdy/native/voice/<operation>` with the usual native session
checks (see [Native workspace API](NATIVE_WORKSPACE_API.md)). The plugin advertises `native-voice-v1`.

## Providers

- **Codex subscription** (default): uses the Codex subscription signed in on the host. The phone never receives
  a provider token.
- **API key**: only when chosen explicitly. Uses the host's `OPENAI_API_KEY` and may cost extra. A subscription
  failure never switches to this mode on its own.

If a call can't be set up, the route returns `409` with `voice_provider_<reason>` (for example
`voice_provider_authentication_failed`, `voice_provider_rate_limited` or `voice_provider_setup_timeout`). The host
logs the same reason. Unknown provider errors become `voice_provider_failed`.

## What the plugin keeps

Nothing lasting. Call state lives in memory and is dropped when the call closes or the plugin unloads. The
plugin doesn't run prompts, store transcripts or schedule work; the conversation lives in Hermes' normal
history.
