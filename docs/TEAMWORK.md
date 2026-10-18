# Teamwork

When Teamwork is on, every agent on the computer knows the other agents and
asks them for help, like coworkers. It's off until someone turns it on in the
bighelp app (Settings › Assistants › Teamwork, or Simple Bot mode's Settings).

## What agents are told

Each agent gets a short brief in its instructions for bighelp chats:

- who it is, and each teammate's name, `@id` and role (from the same profile
  data the app shows: the Bot Mode title and description, else the profile's
  display name and description)
- when to ask a teammate: when part of a request fits the teammate's role,
  accounts or tools better, or when the person asks
- how to ask: `hermes -p <id> chat -Q -q "Message from <Name> (@<id>): <message>"`
  in the terminal (or `message_agent` where Hermes offers it); the answer is
  the command's output
- to say exactly what it needs, share only what the teammate needs, ask
  independent questions at the same time, answer the person once with who
  helped, and never pass a task back or around

The bighelp app shows these conversations in the chat
([Agents talking](AGENT_TALK.md)).

## How it reaches every agent

Hermes loads plugins per agent, and most agents don't load this one, so a
prompt section from the plugin would reach only the default agent. Instead
the brief goes into Hermes' own per-agent setting
`platform_hints.bighelp.append` in each agent's `config.yaml`. Hermes adds it
to the agent's instructions in bighelp chats only. The plugin saves it through
Hermes' own config helpers and lock, the same way the dashboard saves config.

- Text that was already in `platform_hints.bighelp` stays; the brief is added
  after it, and turning Teamwork off removes only the brief.
- The roster stays current: each bighelp chat on the default agent checks at
  most every 10 minutes whether agents were added, renamed or removed, and
  saves new briefs only when something changed. The app's Settings also
  refreshes it.
- A chat reads its instructions when it starts, so open chats keep the brief
  they started with.

State is one file at the Hermes root, `plugin-data/loopdy/team.json`: the
switch and the brief last saved for each agent (so it can be taken out
again). At most 16 agents are listed.

## Route

`POST /api/plugins/loopdy/native/team`, feature `native-team-v1`, with the
usual native headers (`If-Match` context ETag and a lowercase
`X-Loopdy-Request-ID`).

Request: `{"agentId": "default", "enabled": true}`. Leave out `enabled` to
read the status (which also brings the briefs up to date).

Response:

```json
{"enabled": true,
 "agents": [{"id": "default", "name": "Gordie", "role": "Chief of staff"},
            {"id": "nova", "name": "Nova", "role": "Calendar and email"}],
 "reached": ["default", "nova"], "failed": []}
```

- `reached`: agents whose config has the brief now.
- `failed`: agents whose config Hermes didn't save; the app names them.
- Errors: `422` for a bad body, `412 context_changed`, `503 team_unavailable`
  when the feature isn't listed.
