# What the agents did in the background today

The Today page of the bighelp app lists the work that one agent runs in the
background, for the day of the phone: subagents, commands and scheduled tasks.
It shows what runs now, and what started or ended since the start of the day.
[Which agents are working](AGENTS_ACTIVITY.md) has the same work as a count on
the home of the app.

The plugin reads the records that Hermes keeps. It only reads. It never writes
to Hermes. "Background work" in AGENTS_ACTIVITY.md says what each record is,
and when a running piece of work counts. This page has the rules that are only
for Today.

## Route

`POST /api/plugins/loopdy/native/agents/today`, feature
`native-agents-today-v1`, with the usual native headers (`If-Match` context
ETag and a lowercase `X-Loopdy-Request-ID`).

Request:

```json
{"agentId": "nova", "since": 1794038400}
```

- `agentId`: one profile ID.
- `since`: epoch seconds. It is the start of "today" in the time zone of the
  phone. The phone decides the day. The plugin does not know the time zone.
- The body has no other fields. A `since` that is more than 48 hours away from
  the clock of the host, in the past or in the future, is `422 invalid_request`.
  A `since` in the future (for example, a phone clock that runs fast around
  midnight) is read as now.

Response:

```json
{"items": [
  {"id": "delegation:abc123", "kind": "subagent", "title": "Find a battery back plate",
   "state": "running", "startedAt": 1794091440.2, "sessionId": "20261107_164100_ab12cd"},
  {"id": "process:proc_1a2b3c", "kind": "command", "title": "npm run dev",
   "state": "running", "startedAt": 1794091260.0},
  {"id": "delegation:def456", "kind": "subagent", "title": "Look at the puck photo",
   "state": "done", "startedAt": 1794091500.0, "endedAt": 1794091560.0,
   "detail": "Found the 2 mm gap in the case", "sessionId": "20261107_164100_ab12cd"},
  {"id": "message:98765", "kind": "command", "title": "./test-hotspot.sh",
   "state": "failed", "endedAt": 1794082200.0, "detail": "Exit 1",
   "sessionId": "20261107_090000_cd34ef"},
  {"id": "cron:job42:1794038400", "kind": "scheduled", "title": "Morning brief",
   "state": "done", "startedAt": 1794038400.0, "endedAt": 1794038460.0}
], "truncated": false}
```

- `id`: `delegation:<id>`, `process:<id>`, `message:<id>` (a command that
  ended) or `cron:<job>:<start>`. It stays the same while the work goes from
  running to finished, except for a command: a running command is
  `process:<id>`, and its ending is `message:<id>`.
- `kind`: `subagent`, `command` or `scheduled`.
- `state`: `running`, `done`, `failed`, `stopped` or `unknown`.
- `title`: plain text, one line, at most 120 characters. When Hermes gives no
  title, it is "Subagent", "Command" or "Scheduled task".
- `detail`: one line of at most 140 characters, only when there is one. For a
  subagent that finished it is the first line of its summary. For a subagent
  that failed it is the first line of its error. For a command that failed with
  an exit code it is "Exit 1" (with the code). A scheduled task has no `detail`.
- `startedAt` and `endedAt`: epoch seconds. Each is only there when Hermes
  knows it. A command that ended has no `startedAt`: Hermes does not keep it.
- `sessionId`: the chat that the work belongs to. It is only there when it is a
  chat that is stored in this agent's `state.db`.

An item is in the list when it started or ended at or after `since`. Work that
runs now is always in the list, also when it started before `since`.

The list is in this order: the work that runs now first, newest start first.
Then all other items, newest end first (or newest start, when there is no end).
The list has at most 100 items. When there are more, the oldest are left out
and `truncated` is `true`.

## States

| Record | State |
|---|---|
| Subagent `completed` (or `success`) | `done` |
| Subagent `failed`, `error`, `timeout`, `stalled`, `rejected` | `failed` |
| Subagent `interrupted`, `cancelled` | `stopped` |
| Subagent `unknown`, or `running` with an owner that is gone | `unknown` |
| Command in `processes.json` with a live process | `running` |
| Command notice "Background Process Finished" | `done` |
| "Failed", "Failed to Start", "Lost" | `failed` |
| "Terminated" | `stopped` |
| "N Background Processes Finished" | `done`, one item titled "N commands" |
| Scheduled run `running` with a live owner | `running` |
| Scheduled run `completed` | `done` |
| Scheduled run `failed` | `failed` |
| Scheduled run `cancelled` | `stopped` |
| Scheduled run `unknown`, or `running` with an owner that is gone | `unknown` |

A scheduled run that Hermes `missed` never started, so it is not in the list.

## Where the items come from

- **Subagents:** `async_delegations` in `state.db`. A subagent batch is one item.
  Its title is the group name, or else its first goal.
- **Commands that run now:** `processes.json` in the home of the agent.
- **Commands that ended:** when a background command ends, Hermes saves a
  notice in the chat that started it (a message with `display_kind`
  `process_complete`). The plugin reads these notices. Hermes saves a notice
  only for a command that it was told to report (`notify_on_complete`). A
  command that started without it is in the list while it runs, and then it is
  gone: nothing records that it ended. It reads only the chats
  that were active since `since` (at most 100, newest first), and in each chat
  only the newest 300 messages of the window. It never reads the whole
  history. The state comes from the words of the notice, for example
  "Background Process Failed (exit 1): ./test-hotspot.sh".
- **Scheduled tasks:** `cron/executions.db` for the runs, `cron/jobs.json` for
  the job names (or the start of the prompt, for a job without a name). A run
  is linked to its chat by the chat name that Hermes makes
  (`cron_<job>_<date>_<time>`) and the start time. Without such a chat, the
  item has no `sessionId`. The ledger says how a run ended. The plugin
  does not guess: when the ledger has no clear answer, the state is `unknown`.

A record that is missing (an older Hermes) or not readable adds no items. The
other records still give theirs.

## Limits and errors

- The answer is kept for 2 seconds for each agent and `since`.
- The response has at most 100 items and fits in 256 KiB.
- `404 agent_not_found`: the agent is not a profile on this host.
- `422 invalid_request`: a bad body, or a `since` that is out of range.
- `412 context_changed`: the context changed while the route read.
- `503 agents_today_unavailable`: the feature is not in the context list.
- Titles and details are text that agents and commands wrote. The plugin keeps
  the first line, cuts it, and passes it through the redaction of Hermes (with
  URL credentials too, when this Hermes can). When the text cannot be redacted,
  the item has the fallback title and no detail. It never sends the raw text.
  It cannot know what such text means, so it is for people to read. The app
  must not treat it as a path or a command.
- The title of a command is the command line as Hermes saved it (redacted,
  cut to 80 characters). It can name files and folders on the computer, for
  example `python /home/sam/build.py`. A subagent's goal, summary and error can
  do the same. The plugin does not remove paths from this text.
- A command that a subagent started is not a separate item. The subagent is
  the item.
- A command that ended is only in the list when its notice is in a chat that
  was active in the window. A chat that was quiet all day is not read.
