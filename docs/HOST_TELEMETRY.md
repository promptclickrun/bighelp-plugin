# Host telemetry

The plugin sends a small stream of anonymous numbers about how this Hermes host runs: how long a turn takes, which
built-in tool ran, whether an alert arrived. It never sends what a chat, a prompt, a file or a person says. It is on
by default, and any one of the switches below turns it off.

## Turn it off

Any one of these is enough. Nothing is queued or sent while telemetry is off, and turning it off drops what is
waiting at once.

| Switch | How |
| --- | --- |
| `DO_NOT_TRACK=1` | An environment variable of the Hermes process |
| `BIGHELP_TELEMETRY=0` | An environment variable. `false`, `off` and `no` work too. The old name `LOOPDY_TELEMETRY` still works when `BIGHELP_TELEMETRY` is not set |
| Hermes settings | `config.yaml` of the Hermes home: `bighelp:` with `telemetry: false` below it |
| Command | `hermes bighelp telemetry off` (and `on`, and `status`) |
| The bighelp app | Settings > Privacy > Share usage and diagnostics. The app tells each host it connects to |

`hermes bighelp telemetry status` shows each switch, and where the field list is. `hermes bighelp telemetry on`
clears the command's switch and the app's switch. It cannot beat `DO_NOT_TRACK`, `BIGHELP_TELEMETRY` or the Hermes
setting, which are yours to set on the host. The app's switch can turn telemetry off or back on, but it never
overrides one of those.

The command's switch and the app's switch are kept in `plugin-data/loopdy/telemetry-settings.json` at the Hermes
root. When telemetry goes off through either of them, the host's telemetry ID is deleted, and a new one is made if
it is turned on again.

## Where the data goes

1. The plugin sends a batch to `https://telemetry.bighelp.app/v1/batch`. That is a Cloudflare Worker.
2. The Worker checks every value against the lists on this page and drops what does not fit. It does not store or
   log the network address of the host.
3. It forwards the batch to Microsoft Azure (Application Insights), where it is kept for 90 days.

The address is fixed in the code. There is no setting to point it elsewhere.

## What is sent

Every batch carries:

| Field | Value |
| --- | --- |
| `source` | `host` |
| `id` | The host's telemetry ID: a random UUID v4, made on first use and kept in `plugin-data/loopdy/telemetry-id` at the Hermes root. Nothing derives it (not from a key, a name or a machine), and it is not linked to your notification setup |
| `session` | A random UUID v4 made when the Hermes process starts |
| `context.pluginVersion` | The plugin version, for example `3.30.0` |
| `context.hermesVersion` | The Hermes version, for example `0.22.1` |
| `context.osFamily` | `linux`, `macos`, `windows` or `other` |
| `context.python` | The Python version, major and minor, for example `3.14` |
| `context.agentCount` | How many agents the host has, as `1`, `2-5`, `6-20` or `21+` |

Each event has a name, a time in Unix milliseconds, and only the values here:

| Event | When | Values |
| --- | --- | --- |
| `host.started` | The Hermes process has been up for 30 seconds | None |
| `turn.completed` | A turn ends | `durationMs`; `firstTokenMs` (when Hermes tells the plugin that the answer started); `toolCalls` (a count); `outcome` (`ok`, `failed` or `cancelled`); `provider`; `model`; `sessionSource` |
| `tool.completed` | A tool call ends | `tool`; `durationMs`; `outcome` (`ok` or `failed`) |
| `alert.queued` | The plugin queues a notification | `eventType`; `queueMs` (how long it waits before the first try) |
| `alert.sent` | A send to the notification service ends | `eventType`; `relayMs` (how long the service took to answer); `result`; `attempt` (a count) |
| `error` | A fixed error code that the plugin already logs, on the host's side | `code`; `stage` |

How each value is limited:

- **Numbers** are whole numbers in a fixed range (milliseconds up to one day, counts up to 100,000). A value that
  is not a number in range is left out.
- **`tool`** is the name of a built-in Hermes tool or a bighelp tool (`bighelp_*`, `iphone_*`). A tool from an MCP
  server is `mcp`. Every other tool is `custom`. The plugin has the list of names in its code, and it never sends
  a name that is not on it.
- **`provider`** is the name of a well-known public AI provider (for example `anthropic`, `openai` or
  `openrouter`). Any other provider, for example a server on your own network, is `custom`.
- **`model`** is the model ID only for a provider whose model IDs are its own public catalog. For every other
  provider it is `custom`, and so is any model ID that could name a fine-tune, a deployment or an account.
  Nothing is sent when Hermes does not say which provider answered.
- **`sessionSource`** is one of `bighelp`, `cli`, `cron`, `api`, `telegram`, `discord`, `slack`, `whatsapp`,
  `signal`, `email`, `webhook` or `other`.
- **`eventType`** is one of `session.completed`, `session.failed`, `scheduled.completed`, `scheduled.failed`,
  `approval.required`, `clarification.required`, `subagent.completed`, `subagent.failed`, `wake` (a quiet push
  that asks the phone to renew a sign-in) or `liveactivity` (an update of a Live Activity).
- **`result`** is `accepted` or `duplicate`, or the fixed code of a refusal (for example
  `notification_remote_rejected`).
- **`error`** has a code that is a lowercase word chain (for example `native_service_unavailable`) and a stage:
  `native_route` or `notification_send`. Only failures on the host's side are reported, not mistakes of the app.
  The same pair is reported at most once a minute.

## What is never collected

Message text, prompts, replies, tool arguments or output, file names or paths, card contents, push tokens, names,
emails, IP addresses, host names, host URLs, profile names, user names, chat or session IDs, and event IDs. No
free text from Hermes reaches an event: every emitter takes typed values, and the plugin checks each one against a
list in its code before it queues the event. `tests/test_host_telemetry.py` feeds realistic chat text, paths,
names, emails and addresses through every emitter and hook, and checks that none of it is in the batch.

## How it runs

- Events wait in a memory queue of at most 500. When it is full, the oldest events go first. Nothing is written to
  disk.
- A background thread sends a batch every 60 seconds, or at once when 50 events wait. A batch holds at most 50
  events. A send has a timeout of 5 seconds. A send that fails is dropped without a message. After a `429` answer
  the thread waits (as long as the service says, else 2 minutes, doubling up to 1 hour) and keeps the events.
- A turn or a tool call never waits for it, and an error in it never reaches Hermes. When the process ends, the
  plugin makes a last, short try to send what is left.
- One Hermes process sends one stream, though the plugin is loaded more than once in a process. The copies share
  one queue, one thread and one set of timers.
- The plugin reads turns and tool calls from Hermes' own plugin hooks (`pre_llm_call`, `post_llm_call`,
  `post_tool_call`, `on_session_end`). When Hermes has them, it also uses `pre_api_request` (which provider
  answered) and `on_stream_start` (when the answer started). On an older Hermes the plugin leaves those fields out.
- A test run (`unittest` or `pytest`) never sends anything.

## The app's switch (`native-host-telemetry-v1`)

The plugin lists `native-host-telemetry-v1` in `/native/context`. The app calls the routes only when it is there.
Both routes need the usual `If-Match` header (the context ETag) and a lowercase UUID in `X-Loopdy-Request-ID`.
Answers are `no-store`, and echo the request ID.

`GET /api/plugins/loopdy/native/telemetry` answers:

```json
{"version": 1, "enabled": true, "appEnabled": true, "lockedOff": false}
```

- `enabled`: whether telemetry runs now, after all the switches above.
- `appEnabled`: the app's own switch.
- `lockedOff`: `true` when `DO_NOT_TRACK`, `BIGHELP_TELEMETRY` or the Hermes setting forces it off. The app cannot
  change that. When `enabled` is `false`, `appEnabled` is `true` and `lockedOff` is `false`, the command
  `hermes bighelp telemetry off` was used.

`PUT /api/plugins/loopdy/native/telemetry` with `{"enabled": false}` or `{"enabled": true}` sets only the app's
switch and answers like `GET`. The body must have exactly that one boolean.

| Status | Code | When |
| --- | --- | --- |
| 401 | | No sign-in |
| 412 | `context_changed` | The `If-Match` value is old |
| 422 | `invalid_request` | A bad request ID, a query, or a bad or extra body field |
| 428 | `context_required` | No `If-Match` |
| 503 | `host_telemetry_unavailable` | The host doesn't list the capability, or can't save the switch |
