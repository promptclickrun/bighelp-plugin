# Voices of speech plugins

A Hermes plugin can add a speech (TTS) provider, for example Phonon. Only the
plugin knows its voices, so the bighelp app asks this route to show them in
its voice picker.

## How it works

The route reads the provider in the agent's own profile, with the TTS rules of
Hermes itself:

- A built-in provider (`edge`, `openai`, `elevenlabs` and the others) never
  goes to a plugin. A provider of the same name with `type: command` in the
  agent's `tts.providers` also wins over a plugin.
- Only a plugin that the agent loads answers. An agent that does not load
  Phonon gets no Phonon voices.
- The route calls `list_voices()`, `list_models()` and `default_voice()` of
  the plugin. The default voice comes from the agent's own settings when the
  plugin reads them (Phonon does).

A built-in, command or unknown provider gives empty lists, not an error.

## Route

`POST /api/plugins/loopdy/native/voice-catalog`, feature
`native-voice-catalog-v1`, with the usual native headers (`If-Match` context
ETag and a lowercase `X-Loopdy-Request-ID`). It changes nothing.

Request:

```json
{"profile": "nova", "provider": "phonon"}
```

Response:

```json
{"ok": true, "provider": "phonon",
 "voices": [{"id": "Easton", "name": "Easton", "language": "en"}],
 "models": [{"id": "phonon", "name": "Phonon"}],
 "defaultVoice": "Easton"}
```

- `provider`: the provider name in lowercase.
- `voices`: at most 500. `language` is optional.
- `models`: at most 50.
- `defaultVoice`: only when the plugin gives one.
- Each text is at most 160 bytes. The route skips an entry without a good
  ID, and an ID that it already gave. It cuts a long name. All lists together
  stay below 120 KB.

Errors:

- `404 profile_not_found`: the agent is not a profile on this host.
- `503 voice_catalog_failed`: the plugin failed while it listed its voices.
  The log has only the class of the error.
- `422` for a bad body, `412 context_changed`, `503 voice_catalog_unavailable`
  when the feature is not in the list.
