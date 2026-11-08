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
- The body has no other fields. A `since` that is more than 31 days before the
  clock of the host, or more than 48 hours after it, is `422 invalid_request`.
  A `since` in the future (for example, a phone clock that runs fast around
  midnight) is read as now.
- The app can ask for a week or more: it groups the list by day.

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
The list has at most 200 items. When there are more, the oldest are left out
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
- The response has at most 200 items and fits in 512 KiB.
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
- Titles and details show file names only. After redaction, the plugin
  replaces each absolute or home-relative path with its last component:
  `python /home/sam/build.py` is `python build.py`, and
  `cd /srv/app && npm run dev` is `cd app && npm run dev`. This holds for
  commands and for the goal, summary and error of a subagent. Relative paths
  (`./sync.sh`, `src/case.tsx`) and URLs stay as they are. A token that is
  only `/` or `~` stays as it is. The title of a command is the command line
  as Hermes saved it (redacted, cut to 80 characters).
- A command that a subagent started is not a separate item. The subagent is
  the item.
- A command that ended is only in the list when its notice is in a chat that
  was active in the window. A chat that was quiet all day is not read.

## Open one item

`POST /api/plugins/loopdy/native/agents/today/detail`, feature
`native-agents-today-detail-v1`, with the usual native headers. The app calls
it when a person opens an item of the list. The route has the same rules as
the list: it only reads, it has no side effects, and Hermes never changes.

Request:

```json
{"agentId": "nova", "id": "delegation:abc123"}
```

- `agentId`: one profile ID.
- `id`: an item ID that the list returned. The body has no other fields.
  An ID that is not of the four kinds above is `422 invalid_request`.

Response: the same `item` as the list gives for that ID, and one more object.
The object is `subagent`, `command` or `scheduled`, as the kind of the item
says. Each field in it can be absent.

```json
{"item": {"id": "delegation:abc123", "kind": "subagent", "title": "Find a ryokan",
          "state": "running", "startedAt": 1794091440.2},
 "subagent": {
   "goal": "Find a ryokan near Kyoto station",
   "model": "gpt-6-luna",
   "summary": "The answer of the subagent, as Markdown",
   "error": "One line",
   "steps": [
     {"at": 1794091442.0, "kind": "tool", "tool": "web_search",
      "preview": "kyoto station ryokan", "state": "done"},
     {"at": 1794091450.0, "kind": "message", "preview": "I found two places."},
     {"at": 1794091460.0, "kind": "tool", "tool": "read_file",
      "preview": "notes.md", "state": "failed"}],
   "stepsTruncated": false},
 "command": {"command": "npm run dev", "exitCode": 0,
             "output": "The newest output", "outputTruncated": true},
 "scheduled": {"job": "Morning brief", "status": "done",
               "output": "The output of the run, as Markdown", "error": "One line"}}
```

The answer has `item` and exactly one of `subagent`, `command` and `scheduled`.

### Subagent

- `goal`: the goal text (at most 4,000 characters). For a batch, the goals of
  the batch, one after the other.
- `model`: the model that the subagent ran on.
- `summary`: the answer of the subagent as Markdown (at most 16,000
  characters), when it has finished. For a batch, the answers of its subagents
  in order, each under its goal.
- `error`: one line (at most 300 characters). It is there when a subagent
  failed.
- `steps`: what the subagent did, oldest first. At most 300. When there were
  more, the newest 300 stay and `stepsTruncated` is `true`.
  - `kind` is `tool`, `message` or `thinking`. A `message` is text that the
    subagent wrote between tools (the first 300 characters). `thinking` is only
    there when Hermes saved the reasoning. The final answer is the `summary`,
    never a step.
  - For a tool: `tool` is the name of the Hermes tool. `preview` is the one
    argument that tells most: a query, a web address as host and path (no
    scheme, no credentials, no query), a file name, or the first 80 characters
    of a command.
  - `state` is `done` or `failed`, from the result that Hermes saved (a failed
    terminal exit code, an `error`, `success: false`, a failed status, or a
    result that starts with "Error:"). It is `running` when no result has come
    yet and the subagent is still running. For a subagent that has ended, a
    call with no result has no `state`.

The plugin reads what Hermes saved for the subagent while it works, so a
running subagent shows its steps so far.

Hermes does not write which chats belong to a delegation. The plugin finds the
chats of the subagents (`source` `subagent`) like this: they are under the chat
that started the work, they began at the time of the work, and their first
message is one of the goals of the delegation. A subagent chat that Hermes
compressed goes on in a new chat with the same `_subagent_id`, and the plugin
follows it. The steps of all the subagents of a batch are in one list, in time
order. Two delegations that began together with the same goal in the same chat
cannot be told apart: each shows the steps of both.

### Command

- A command that runs now: `command` is the command line. `output` is the end
  of its output file (`logs/process-output/<id>.log` in the home of the agent).
  The plugin reads at most the last 64 KiB, and `outputTruncated` says that
  more came before. Hermes only keeps an output file for a command that a
  worker started. For any other command, there is no `output`. A command that
  runs has no `exitCode`.
- A command that ended: the plugin reads the notice that Hermes saved in the
  chat. It has `command`, `exitCode` (only when the notice has one) and
  `output`. When Hermes cut the output of the notice, or the plugin cut it to
  16,000 characters, `outputTruncated` is `true`. A notice for several commands
  at once has only `output`.
- `output` is the newest text, at most 16,000 characters. It has no ANSI
  codes. A line that a program wrote again (a progress bar) shows its last
  state.

### Scheduled task

- `job`: the name of the job, when it has one.
- `status`: the state of the item, as the ledger of Hermes says it. It is
  never a guess.
- `output`: the output of the run as Markdown (at most 16,000 characters).
  It is the final answer of the chat of the run. When the run has no chat,
  it is the response in the output file that Hermes saved. A file does not
  say which run it belongs to, so the plugin takes it only when the run is
  the one that finished nearest to the file, within 60 seconds. It never
  shows a neighbour's file. A run that is going has no `output`.
- `error`: the error that the ledger has for the run, on one line.

### Privacy

All text goes through the same redaction as the list, with the same
file-name rule: no folder of the computer shows (`python /home/sam/build.py`
is `python build.py`). This holds for the goal, the summary, every preview,
and all output. When text cannot be redacted, the plugin leaves that field
out. It never sends the raw text. Hermes writes the files that the plugin
reads. The plugin reads an output file only when it is a `.log` file, not a
link, in the `logs/process-output` folder of the agent.

### Limits and errors

- The answer is kept for 2 seconds for each agent and item.
- The response fits in 512 KiB. When a long text makes it larger, the oldest
  steps go first and `stepsTruncated` is `true`.
- `404 agent_not_found`: the agent is not a profile on this host.
- `404 activity_not_found`: the agent has no such item. The item can be gone
  (a command that stopped, a record that Hermes removed), or the plugin cannot
  read it now. The answer does not say which.
- `422 invalid_request`: a bad body.
- `412 context_changed`: the context changed while the route read.
- `503 agents_today_detail_unavailable`: the feature is not in the context
  list.
