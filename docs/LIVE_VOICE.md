# Live voice

Live voice lets you talk with an agent in real time from the bighelp app. The plugin only sets up the audio
call and relays its events. The actual work runs as an ordinary Hermes chat, so Hermes stays in charge of
tools, approvals and history.

On the call, the voice model is the agent itself. It speaks in the first person with the agent's name and
character, and it never talks about "your agent" or "Hermes".

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

- **Codex subscription** (default): GPT-Live on the Codex subscription signed in on the host. The phone never
  receives a provider token.
- **API key**: GPT-Live, only when chosen explicitly. Uses the host's `OPENAI_API_KEY` and may cost extra. A
  subscription failure never switches to this mode on its own.
- **Gemini**: Gemini Live with the agent's own `GEMINI_API_KEY` (or `GOOGLE_API_KEY`), read in the agent's
  profile like its other credentials. Free AI Studio keys work. The plugin advertises `native-voice-gemini-v1`.

The app can pick a different provider and voice for each agent. The plugin takes them from each `status` and
`offer` request.

## Gemini Live

A Gemini call has no media or events on the host. `offer` (with `provider: "gemini"` and no `sdp`) makes a
single-use Gemini token and returns it with where to connect:

```json
{"voiceId": "...", "transport": "websocket", "url": "wss://generativelanguage.googleapis.com/ws/...Constrained",
 "token": "auth_tokens/...", "model": "models/gemini-3.8-live", "expiresAt": 1793402836}
```

The token is good for one session that starts within 60 seconds and lasts up to 30 minutes. It locks the whole
session: the model, the voice, the agent's brief, the tools, both transcripts and context compression (without
it, an audio call ends at about 15 minutes). The app connects with `Authorization: Token <token>` and sends a
setup that names only the model. The Gemini key never leaves the host.

The session has Google Search and two functions that the app answers:

- `start_work(request)`: the app answers at once with a started result, sends the request as a normal message in
  the agent's chat, and later sends the reply as a text message that starts with `UPDATE`.
- `check_work()`: the app answers with the work it has in progress.

Gemini answers an unknown voice name with its default voice instead of an error, so the plugin accepts only the
30 voices in `gemini_live.VOICES`. Calls for Gemini cannot `poll` or post a `result`, and `close` only spends the
voice ID.

If a call can't be set up, the route returns `409` with `voice_provider_<reason>` (for example
`voice_provider_authentication_failed`, `voice_provider_rate_limited` or `voice_provider_setup_timeout`). The host
logs the same reason. Unknown provider errors become `voice_provider_failed`.

The Codex sign-in is read inside the selected agent's Hermes profile (its home and, on Hermes 0.21.4+, its
credentials). Hosts serving several profiles refuse credential reads outside one; if that still happens the call
fails with `voice_provider_credentials_unavailable`, not as a sign-in failure.

## What the voice model says

The plugin builds the voice model's instructions for each call (`voice_brief.py`). They start with the agent's
display name and up to 6,000 bytes of its `SOUL.md`, so the model speaks as that agent, in the first person: "I'm
on it", "I finished it". It never says "your agent", "the agent" or "Hermes".

The brief also names the language to speak: the first language in the phone's `Accept-Language` header, else
English. Voice models otherwise choose the language from the audio, and noise or one unclear word made a model
answer an English speaker in Spanish. The model changes language only when the person clearly speaks to it in
another one, in a full sentence.

When a request sounds complete, the model first says one short line in its own words ("On it, checking now"),
then starts the work in the same response: a delegation on GPT-Live, `start_work` on Gemini. It never claims the
work is done and doesn't guess a result; it speaks the result only once it arrives, as its own work. On the Codex
subscription the plugin also sets `delegation.ack_filler` to `false`, so the provider adds no filler of its own
on top of the model's line.

## What the plugin keeps

Nothing lasting. Call state lives in memory and is dropped when the call closes or the plugin unloads. The
plugin doesn't run prompts, store transcripts or schedule work; the conversation lives in Hermes' normal
history.
