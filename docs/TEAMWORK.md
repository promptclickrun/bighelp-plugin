# Teamwork

When Teamwork is on, every agent on the computer knows the other agents and
asks them for help, like coworkers. It's off until someone turns it on in the
bighelp app (Settings › Assistants › Teamwork, or Simple Bot mode's Settings).

## What agents are told

Each agent gets a short brief in its instructions for bighelp chats:

- who it is, and each teammate's name, `@id` and role (from the same profile
  data the app shows: the Bot Mode title and description, else the profile's
  display name and description)
- to bring teammates in on its own: before it starts on a request, check the
  roster, and when a teammate's role covers the request or part of it, hand
  that part over without being told, asking or only suggesting it; split work
  that spans roles and ask those teammates at the same time; do it itself only
  when it's quick, conversation, nobody fits better, or the person asks for it
- how to ask: the `message_agent` tool (Hermes Bot Mode), which reaches a
  teammate's Bot Chat even while bighelp has it open; the reply arrives later
  as a message. Without it,
  `env -uHERMES_SESSION_SOURCE hermes -p <id> chat -Q -q "Message from <Name> (@<id>): <message>"`
  in the terminal, which starts a new chat (never the teammate's open Bot
  Chat, which Hermes would refuse); the answer is the command's output.
  `env -u` removes the `bighelp` session source that the asking chat's
  terminal has: Hermes refuses a new chat with that source (`invalid_params`).
  There is no space after `-u`, so the app still shows the command as a
  message to that teammate
- that the asking agent still owns the person's final answer: a queued
  message isn't an answer
- to brief a teammate like a coworker, share only what the task needs, tell
  the person in one line who it's asking, answer once with who helped, answer
  teammates who ask it, and never pass a task back or around
- to explain all this when the person asks how it works with its team

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
- The brief sits between two marker lines (`<!-- bighelp Teamwork: … -->` and
  `<!-- end bighelp Teamwork -->`). Cloning an agent copies its `config.yaml`,
  brief included, so a new agent starts out told it is the agent it was copied
  from. Each save takes out every marked block, and every unmarked brief from
  3.24.x and earlier (recognized by its fixed first and last lines), before
  adding the agent's own. A clone is corrected on the next refresh, since a new
  agent changes the roster. Everything else a clone copied stays.
- The roster stays current: each bighelp chat on the default agent checks at
  most every 10 minutes whether agents were added, renamed or removed, or
  whether a plugin update changed the brief's words, and saves new briefs only
  when something changed. The app's Settings also refreshes it.
- A chat keeps the instructions it started with. So a bighelp chat on the
  default agent that started before Teamwork (or before the roster changed)
  gets the current brief once, through the plugin's `pre_llm_call` context:
  Hermes saves that note with the message (`api_content`) and replays it on
  every later turn. The plugin sends it only when the chat's instructions and
  saved notes don't already hold the current brief, never on a chat's first
  turn (its instructions have it) or to helpers. A Hermes that doesn't save
  notes gets it every turn. Other agents' open chats get it when they start
  again, because they don't load this plugin.

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
