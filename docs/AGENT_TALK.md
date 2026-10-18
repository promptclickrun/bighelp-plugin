# Agents talking to each other

An agent can ask another agent on the same computer for help: it runs
`hermes -p <agent> chat -q "..."` in its terminal, or Hermes' `message_agent`
delivers a message to the other agent's Bot Chat. The bighelp app shows each
of these as one line in the chat, with both agents' pictures, and a tap shows
what they said to each other.

The app reads the message and the answer from the tool call itself. This
feature adds what the app can't see from there: what the other agent is doing
while the first one waits ("Nova is searching the web..."), and which of the
other agent's chats the exchange ran in.

## How it works

The other agent answers in its own chat, and Hermes saves each step there
before it runs. The route reads that agent's history, read-only:

1. The app sends, for each call it sees, the agent written to and a SHA-256
   hash of the message: the words with Hermes' "Message from Name (@handle):"
   signature removed and the ends trimmed. The words never leave the phone.
2. In that agent's `state.db`, the route looks at chats started in the last
   3 days (at most 64) and its Bot Chat, and hashes their recent messages
   from someone else the same way.
3. For each match it reads the steps after the message: a tool call without
   its result yet is the tool running now. A reply, the next message, an ended
   chat, or 15 minutes without a step mean the agent is no longer on it.

Nothing is stored. Only agents on this computer are read: `hermes peer dm`
reaches another computer's Hermes.

## Route

`POST /api/plugins/loopdy/native/agents/talk`, feature `native-agent-talk-v1`,
with the usual native headers (`If-Match` context ETag and a lowercase
`X-Loopdy-Request-ID`).

Request:

```json
{"agentId": "default", "sessionId": "20261017_170034_98063e",
 "calls": [{"agentId": "nova",
            "message": "3f8b2abb9596992352771eb54daf4895b0b8beca852321f474bca45b96e49a87"}]}
```

- `agentId`, `sessionId`: the chat's agent (Hermes profile) and its stored
  Hermes session ID. An unknown `agentId` is `404 profile_not_found`.
- `calls`: at most 16, each the agent written to (a profile ID) and the
  message's lowercase SHA-256 hash. An agent that isn't a profile here is
  skipped.

Response, newest first, at most 32 runs and 4 per message:

```json
{"runs": [{"agentId": "nova", "sessionId": "20261017_170115_941fc0",
           "message": "3f8b2abb...", "startedAt": 1760720475.2,
           "working": true, "tool": "web_search"}]}
```

- `startedAt`: when the message reached that agent's chat (Unix seconds).
- `tool`: the Hermes tool it's running, only while `working`.
- Errors: `422` for a bad body, `412 context_changed`, `503
  agent_talk_unavailable` when the feature isn't listed.

The app reads the route every 2 seconds while one of its calls runs or an
agent it asked is still working, and once when the calls in a chat change.
With an older plugin the line in the chat says only who is being asked.

## Limits

- A message sent from a file (`--query-file`) has no words in the call to
  hash, so its run can't be found.
- The same message sent twice matches both runs; the app takes them in order.

## A chat busy in another process

The asked agent answers in its own `hermes -p <agent> chat` process, so when
the person opens that agent's chat (its Bot Chat) in bighelp, the app's live
connection hears nothing. Feature `native-chat-activity-v1` tells the app the
chat is busy, so it shows the agent typing, a Stop button, and reads the chat
again as it goes.

`POST /api/plugins/loopdy/native/chat/activity` with
`{"agentId": "<profile>", "sessionId": "<stored session ID>"}` answers
`{"working": true, "tool": "web_search", "since": 1760720475.2, "canStop": true}`.

- Hermes keeps a lease for each chat a process holds
  (`<agent home>/runtime/active_sessions.json`). A lease counts when Hermes'
  own check says its process is alive and is still the process that took it
  (pid and start time), and its surface isn't the dashboard's own chats (`tui`,
  `desktop`).
- It's busy when that process took the chat after its last message, or the
  chat's latest turn hasn't answered yet (with the tool it runs, read-only from
  the agent's `state.db`, as above). An open CLI chat that's idle isn't busy.

`POST /api/plugins/loopdy/native/chat/stop`, same body, answers
`{"stopped": true}`: it sends that process Ctrl-C (SIGINT), as stopping it in
its terminal would; Hermes ends the turn and the process. Only a lease with a
recorded start time is signalled, so a reused pid never is. `canStop` says so
up front.

