# Bot Chats that Hermes keeps

Each agent has one Bot Chat: the chat named "Bot Chat". You talk to the agent
there in the bighelp app, and other agents send their messages there
(`message_agent`, peer messages, scheduled deliveries).

The app made each Bot Chat itself, over the socket of the dashboard. That chat
has the source `bighelp`. Hermes delivers the messages of other agents only
into a Bot Chat that is its own local chat (an ID that starts with `local-`)
or older local history (`cli`, `tui`, `desktop` and similar). Thus the first
time that another agent sent a message, Hermes renamed the chat of the app to
`Bot Chat (bighelp <id>)`, made it visible, and started a new Bot Chat. The
Hermes log shows "Bot Chat title held by … moving it aside".

Now the app asks the plugin, and Hermes makes the Bot Chat.

## How it works

1. The route looks in the agent's own `state.db` (read-only) for the chat with
   the title "Bot Chat". If there is one, the route gives it back. It does not
   rename, move, hide or copy it, also when the app made it before.
2. If there is no Bot Chat, the plugin connects to the gateway of that agent
   with the public client of Hermes (`connect_gateway`), as
   `hermes -p <agent> chat -c "Bot Chat" --create-if-missing` does. If the
   gateway does not run, Hermes starts it, and the gateway stops by itself
   when it is idle.
3. The plugin sends `session.create` with the values that the Bot Chat
   delivery of Hermes uses (`_create_bot_chat`): the title "Bot Chat", source
   `cli`, the home folder, and `follow_profile_config`. The chat is also
   hidden, as Hermes Desktop makes a Bot Chat. Hermes finds or makes the chat
   under its own lock and writes it. The plugin writes nothing.
   A gateway that does not know `hidden` yet refuses it before it writes
   anything. The plugin then asks once more without it, and the chat is
   visible, as the Bot Chat delivery of Hermes makes it.

The request ID is `bighelp-bot-chat:<idempotencyKey>`. Hermes gives back the
same chat when the app sends the same key again, for example after a lost
answer.

## Route

`POST /api/plugins/loopdy/native/bot-chat/create`, feature
`native-bot-chat-v1`, with the usual native headers (`If-Match` context ETag
and a lowercase `X-Loopdy-Request-ID`).

Request:

```json
{"profile": "nova", "idempotencyKey": "0b5c8a5e-3f7d-4c1e-9a2b-7d6e5f4a3b2c"}
```

- `profile`: the agent's profile ID.
- `idempotencyKey`: a lowercase UUID. Use the same key for a retry of the same
  request.

Response:

```json
{"ok": true, "storedSessionId": "local-3f…", "created": true, "native": true}
```

- `storedSessionId`: the Bot Chat. The app opens it with `session.resume`.
- `created`: `true` when Hermes made the chat for this key, also for a retry.
  `false` for a Bot Chat that was there before.
- `native`: `true` when the chat is a local chat of Hermes (`local-…`). A Bot
  Chat that the app made before is `false`. Hermes can move that chat aside
  when another agent sends a message.

Errors:

- `404 profile_not_found`: the agent is not a profile on this host.
- `503 bot_chat_gateway_unavailable`: the gateway of the agent is not ready,
  for example during an update. Try again.
- `503 bot_chat_timeout`: the gateway did not answer in time. Try again with
  the same key.
- `409 bot_chat_refused` or `503 bot_chat_refused`: the gateway refused.
  `details.reason` is the fixed code of Hermes, for example
  `permission_denied`. A `503` is temporary. On a host without a model in
  its settings, Hermes cannot keep the new chat (`storage_unavailable`), as
  for its own Bot Chat.
- `422` for a bad body, `412 context_changed`, `503 bot_chat_unavailable`
  when the feature is not in the list.

The feature is in the list only on a Hermes with the gateway client (Hermes
0.22 and later).
