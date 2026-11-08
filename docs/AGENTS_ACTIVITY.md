# Which agents are working

The bighelp app shows on its home which bots are working on a turn and which
bots need you. Simple Bot mode shows it on each bot, and the Agents screen
shows it next to each agent. The plugin tells the app the state of each
agent's Bot Chat in one read, for many agents at a time.

Stock Hermes tells the app only about the chats that the app's own dashboard
runs. It does not tell the app about a turn in another Hermes process, for
example when another agent asks this agent with `message_agent` or with
`hermes -p <agent> chat -q`. It also does not tell the app when such a turn
waits for an approval.

## How it works

For each agent, the route reads the agent's `state.db` (read-only) and the
state of this dashboard. It does not read the history of an agent that is not
working.

- **Working:** Hermes keeps a turn lease (`session_turn_leases`) for each turn
  while the turn runs. This is true for each Hermes process that runs a turn:
  the dashboard (turns that you send from the app, and `message_agent`
  messages to a Bot Chat that is open in the app), the background run of
  `message_agent`, and `hermes -p <agent> chat -q`. These turns count:
  - a turn in the agent's Bot Chat (the chat named "Bot Chat")
  - a `cli` turn in a different chat of the agent (`chat -q` starts a chat of
    its own)

  Other chats do not count, for example chats from messaging apps, scheduled
  tasks and the other chats of the app. A lease counts only while it is
  current and its process is alive (the check of Hermes itself). The route
  also gives the tool that the turn runs now, if there is one. It reads this
  from the last tool call of the turn that has no result yet.
- **Waiting:** Only the process that runs a turn knows that the turn waits for
  you. For the turns that this dashboard runs, the dashboard's state shows an
  approval, a question, a sudo password or a secret that waits for you in the
  Bot Chat. Waiting has priority over working. A `chat -q` run never waits:
  Hermes answers its approvals itself.
- **Idle:** all other agents. An agent without a `state.db` is idle, because
  nothing ran yet.

On a Hermes without turn leases, the route uses the running chats of the
dashboard and the chat leases of Hermes (`native-chat-activity-v1`) instead.

The route keeps each answer for one second, so that many phones can ask
together. It stores nothing.

## Background work

With feature `native-agents-background-v1`, each row also says how much work
the agent runs in the background. The work can be in any chat of the agent, and
in any Hermes process. The count is not part of `state`: an agent can work on a
turn and also have background work.

Three kinds of work count. The plugin reads the record that Hermes keeps for
each kind. It only reads. It never writes to Hermes.

- **Subagents** that run in the background (`delegate_task`). Hermes keeps them
  in the `async_delegations` table of the agent's `state.db`. A row that says
  `running` counts only while the process that owns it is alive (the same check
  as Hermes). When that process is gone, the row is not running work. Hermes
  marks it `unknown` later. One delegation counts as one, also when it has
  many subagents.
- **Commands** that run in the background (`terminal` with `background`).
  Hermes lists the running ones in `processes.json` in the agent's home. An
  entry counts only while its process is alive with the start time that Hermes
  wrote. A process number that another process now uses does not count. Two
  more kinds of entries do not count: a command that a subagent started (the
  subagent is the work), and a command of a chat that is not a chat of this
  agent. Other Hermes processes write to the same file.
- **Scheduled tasks.** The execution ledger of Hermes (`cron/executions.db`)
  records each run with the process that owns it. A run counts while the ledger
  says `running` and its process is alive with the start time that Hermes
  wrote. When Hermes wrote no start time, only the Hermes process that runs the
  plugin counts (this is the check of Hermes). `cron/jobs.json` gives the job name.

If a record is missing or is not readable, it adds nothing. The other records
still count.

The row has `background` only when the count is more than zero:

```json
{"agentId": "nova", "state": "idle",
 "background": {"count": 3, "title": "Find a battery back plate"}}
```

- `count`: the number of pieces of work that run now.
- `title`: the plain title of the piece with the newest start. For a subagent
  it is the first line of its goal. For a command it is the command, cut to 80
  characters (the same as Hermes). For a scheduled task it is the job name
  (or the start of its prompt, when it has no name).
  Titles are one line, and pass through the redaction of Hermes (with URL
  credentials too, when this Hermes can), and show file names only: a path
  such as `/home/sam/build.py` is `build.py`. The row has no `title` when no usable
  title exists, and also when the text cannot be redacted: the plugin never
  sends text that it could not redact. On this row a title has at most 60
  characters, so that 64 agents fit in one answer. The Today route keeps 120.
- Only the feature in the context list turns this on. A request to a plugin
  without the feature gets rows without `background`.

## Route

`POST /api/plugins/loopdy/native/agents/activity`, feature
`native-agents-activity-v1`, with the usual native headers (`If-Match`
context ETag and a lowercase `X-Loopdy-Request-ID`).

Request:

```json
{"agentIds": ["default", "nova", "pip"]}
```

- `agentIds`: 1 to 64 profile IDs. More than 64 is `422 invalid_request`.

Response, in the order of the request:

```json
{"agents": [{"agentId": "default", "state": "idle"},
            {"agentId": "nova", "state": "working", "tool": "web_search",
             "background": {"count": 2, "title": "npm run dev"}},
            {"agentId": "pip", "state": "waiting"}]}
```

- `state`: `idle`, `working` or `waiting`.
- `tool`: the Hermes tool that the turn runs now. It is only there while the
  state is `working`, and only when a tool runs.
- `background`: the background work of the agent. See "Background work".
  It is only there with `native-agents-background-v1`, and only when the count
  is more than zero.
- The response does not include an agent that is not a profile on this host,
  or an agent whose `state.db` the route cannot read now. The app shows
  nothing for such an agent. It does not show it as idle.
- Errors: `422` for a bad body, `412 context_changed`, `503
  agents_activity_unavailable` when the feature is not in the list.

The app reads the route while its home or its Agents screen is on the screen
and the app is active: every 3 seconds while an agent works or waits, else
every 6 seconds. It stops when the screen closes or the app goes to the
background. With an older plugin, the app uses the stock signals of Hermes and
shows no update message.

## Limits

- A turn that waits for you in a different Hermes dashboard process, or in an
  interactive terminal chat, shows as working, not waiting.
- When Hermes runs turns in a separate compute process
  (`dashboard_process_isolation.turn_isolation`), the dashboard keeps a copy
  of the open request. The route reads that copy.
