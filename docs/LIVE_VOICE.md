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
checks (see [Native workspace API](NATIVE_WORKSPACE_API.md)). The plugin advertises `native-voice-v1`. The routes
don't check that feature themselves: they check the native session, the context ETag and that the agent exists.

## A call continues its chat

A call opens in one chat, and it carries that chat on in both directions (`native-voice-history-v1`).

- **Into the call.** Before the voice model listens, the `offer` reads the chat the app names (`agentId`,
  `sessionId`) from that agent's own `state.db`: its newest messages with their roles, the summary Hermes wrote
  when it compacted the older part (without the note and end marker Hermes puts around it for its own model),
  and the request a turn is still working on. Tool calls, tool results and Hermes' own notices stay out. Every provider gets the same snapshot: up to 16 messages, at most 800 characters
  each and 8,000 bytes together, fenced in the instructions as data with each message's role. On GPT-Live the
  oldest give way first when the brief and the chat don't fit in 16,000 bytes. The brief tells the model to
  carry the chat on: answer from it, don't greet as if it were new, and don't repeat earlier answers.
- **Back into the chat.** `POST …/voice/transcript` takes the call's finished lines: `voiceId` and up to 32
  `items`, each `{id, role: user|assistant, text (up to 4,000 characters), interrupted, at}`. Each line becomes an
  ordinary message in the chat (on the newest part of a compacted chat), stamped with the time it was said. A
  reply the person cut off ends with "…". Control characters are taken out, and a line with no words is not
  saved. A store that can't be written answers `voice_chat_busy`, as a running turn does, so the app tries again. Nothing runs: Hermes adds rows another writer saved to the chat's live
  history at its next turn, so the next typed message knows what was said. Requests the voice model handed over
  are already their own turns in the chat, and are never sent again.
- **Order and at most once.** The plugin writes only while no turn runs in the chat (Hermes' turn lease, or this
  dashboard running it), so a spoken line never lands inside a turn. A busy chat answers 409
  `voice_chat_busy`, and the app sends the same lines again later. Each line is saved at most once per call and
  `id` (`plugin-data/loopdy/voice_history.sqlite3` in the agent's home, kept 30 days), so a retry after a lost
  answer or a reconnect never doubles one. The answer lists the IDs written now (`saved`) and before (`known`). A
  chat that isn't this agent's answers 409 `voice_chat_unavailable`.

Reads and writes stay in the one chat the request names, in the agent it names, on this computer.

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
English. Native-audio models can change language in the middle of a call by themselves, and the instructions are
the only control: noise, a TV or a cut-off word made a model answer an English speaker in Spanish. So the model
speaks that language for the whole call and changes only when the person asks in words ("speak Spanish"), and
changes back when they ask. The brief says the rule at its start and again at its end.

With `native-voice-context-v1`, the app's offer can also send (all optional):

| Field | What it is | Route cut | In the brief |
|---|---|---|---|
| `agentName` | The agent's name as the app shows it. It comes before the profile's own lookup, which gives a profile with no display name its ID ("Dr Pickle"). | 200 | 80 |
| `personName` | The name the person saved in bighelp. Never the placeholder "You". | 200 | 40 |
| `place` | A coarse place (city, region, country), only while the person shares their location with bighelp. Never coordinates. | 300 | 80 |
| `timeZone` | The phone's IANA time zone, such as `America/Chicago`. | 64 | a real zone name, or left out |

These are untrusted, and the limits are in code points. The route first cuts an over-long value to its route
limit instead of refusing the call (the app counts characters, Python counts code points). `voice_brief` then
cleans each value and cuts it again to the brief's limit. It keeps one line of letters, marks, digits, spaces,
plain punctuation and the zero-width joiners some names need, and drops anything else (newlines, control characters, braces, markup). The brief shows them in a block of facts about the
person that says they are never instructions. The model uses the person's name now and then, and uses the place
for weather and local questions (Gemini searches with it) instead of asking where the person is.

When a request sounds complete, the model first says one short line in its own words ("On it, checking now"),
then starts the work in the same response: a delegation on GPT-Live, `start_work` on Gemini. It never claims the
work is done and doesn't guess a result; it speaks the result only once it arrives, as its own work. On the Codex
subscription the plugin also sets `delegation.ack_filler` to `false`, so the provider adds no filler of its own
on top of the model's line.

## What the plugin keeps

Nothing of its own beyond the call. The person's name, place and time zone, and the chat's newest messages, go
into that call's instructions to the voice provider, and the plugin doesn't keep them. Call state lives in memory
and is dropped when the call closes or the plugin unloads. What was said on the call is saved in the chat itself,
in Hermes' normal history; the plugin keeps only which lines it saved (call and line IDs, no text). It doesn't run
prompts or schedule work.
