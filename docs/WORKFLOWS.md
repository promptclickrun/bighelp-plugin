# Workflows

A workflow is a set of steps (stages) that your Hermes agents work through one at a time: for example
one agent researches, one writes a draft, one reviews it, and you approve the final file. The bighelp app builds
and starts workflows. The plugin runs them on the Hermes computer, so runs keep going when you close the app.

Nothing runs by itself. A run starts only when you tap Run, and a failed or interrupted stage never retries by
itself: the app shows what happened and offers Try again.

## How it runs

- Workflows are stored for the whole computer, not per agent, in `plugin-data/loopdy/workflows/` under the Hermes
  root. Each role in a workflow (Researcher, Writer, …) is bound to one agent (Hermes profile) on that computer.
- A small coordinator process runs the stages. The plugin starts it when there is work, with `launchctl submit`
  (macOS) or `systemd-run --user` (Linux), so it survives the dashboard and the app. Where neither exists (a
  container or a hosted Hermes) or the service manager refuses, it starts as a detached process in its own session.
  Its lock file (`coordinator.lock`) and the ledger's launch time stop a second coordinator. It stops by itself after
  ten idle minutes. After a reboot or a container restart it starts again the next time the gateway or dashboard
  starts, and a stage that was running then becomes "needs attention": nobody knows how it ended.
- The coordinator starts with a scrubbed environment (`env -i`). The process that starts it works out how to run
  Hermes and hands that over: `$HERMES_BIN` or a `hermes` launcher when that is how Hermes runs, or else this
  interpreter's `-m hermes_cli.main` with Hermes' folder (and its dependency folder, when Hermes manages one) on
  the stage's `PYTHONPATH`, the same way Hermes' own Kanban workers start. Some Hermes launchers run a bundled
  Python that finds Hermes only in-process; without this, every stage stopped at once.
- Each agent stage runs one Hermes chat turn as that agent:
  `hermes -p <agent> --cli chat --source workflow --toolsets <tools> --query-file brief.md --format stream-json`,
  with `HERMES_QUIET_TURN_REPORT_FILE` set so Hermes writes how the turn ended. The chat's session source is
  `workflow`, so the app can keep these sessions out of its chat lists. Older Hermes uses the text runner (below).
- What a stage's Hermes writes to stderr goes to `stderr.log` in the stage's folder, cut to its last 4 KB. It is
  never sent to the app. When a stage stops before its turn began (non-zero exit and no output), the run gets one
  fixed sentence about why, for example "Hermes couldn't load the Python module hermes_cli. (exit code 1)": only a
  missing module's name or a Python error's class name is taken from stderr.
- At most two runs work at the same time (slots). Runs that wait for you use no slot. More runs wait as `planned`,
  oldest first, up to 20.
- A stage gets only the tools listed for it. A stage with no tools runs with only the to-do tool. Messaging,
  scheduled tasks, Kanban, delegation, Home Assistant, `clarify` and the all-in-one platform toolsets are never
  allowed, because a stage must not reach people or start other work. `terminal` is allowed with a warning.
- A stage has a time limit (20 minutes by default, at most 60). At the limit the plugin stops the agent (SIGTERM to
  its process group, then SIGKILL 30 seconds later) and the run fails at that stage.
- Storage limits: 50 MB of files per run, finished runs are removed after 30 days, at most 500 live lines per
  attempt.

### Which Hermes gets which runner

The plugin asks Hermes once which chat flags it has, and passes only those.

| Hermes | Runner | What you get |
| --- | --- | --- |
| 0.21.4 and later | stream | `--query-file brief.md --format stream-json` and the turn report: live lines, token counts, the exact reply |
| 0.21.1 to 0.21.3 | text | `--query-file brief.md -Q`: the final reply comes on stdout at the end. Live lines say only that the stage started and still works, once a minute. Token counts are unknown (`null`) |
| A Hermes without `--query-file` | text | `-q` with the brief itself when it is at most 96 KB, else a short prompt to read `brief.md` in the working folder. The brief is then visible in the process list |

The text runner takes the exit code and the reply on stdout as the result; the hand-off block is read from it as
usual. After a coordinator restart a text-runner stage whose process is gone always needs attention, because
there is no turn report to say how it ended. `--source` and `--cli` are left out when Hermes lacks them; the
session then shows in Hermes' normal chat lists. A Hermes without `--toolsets` can't limit a stage's tools, so
agent stages can't run there (validation issue `tool_scope_unsupported`).

## The hand-off contract

The agent's brief (`brief.md`) ends with how to hand off. The final reply must contain exactly one fenced block
tagged `bighelp-handoff` with JSON:

````
```bighelp-handoff
{"outputs": {"draft": {"content": "# Title\n\nText…"}, "word_count": 912}}
```
````

| Output type | JSON value | Limit |
| --- | --- | --- |
| `markdown_file` | `{"content": "…"}`, or `{"path": "out/name.md"}` for a file the agent wrote in its `out/` folder | 512 KB of UTF-8 text |
| `text` | a string | 8 KB |
| `number` | a finite number | |
| `decision` | one of the output's `values` | |
| `notes` | `[{"severity": "minor" or "major", "text": "…"}]` | 20 notes, 1,000 characters each |

A stage passes when the agent exits with code 0, the reply has exactly one valid block, and every declared output is
there with the right type. Extra outputs are ignored. The plugin stores each output by its SHA-256 and gives later
stages read-only copies.

## Definition (schemaVersion 1 and 2)

```json
{
  "schemaVersion": 1,
  "name": "Weekly newsletter",
  "description": "Research, draft and review the weekly newsletter.",
  "roles": [{"key": "writer", "label": "Writer"}],
  "inputs": [{"key": "topic", "label": "Topic", "type": "text", "required": true}],
  "limits": {"stageMinutes": 20, "maxRevisions": 2},
  "stages": []
}
```

- Keys (roles, inputs, stages, outputs) match `^[a-z][a-z0-9_]{0,31}$`. Names and titles are 1 to 80 characters.
- `inputs[].type`: `text` (2,000 characters), `long_text` (20,000), `number`, `choice` (needs `choices`, 1 to 20
  strings). `sample` is an optional example value.
- `limits.stageMinutes` 1 to 60, `limits.maxRevisions` 0 to 5. At most 20 stages, 64 KB in all.
- References are `inputs.<key>` or `<stageKey>.<outputName>` and point only to earlier stages.

### schemaVersion 2: the stage graph

Version 1 workflows stay valid and mean "each stage goes to the next one in the list". Version 2 adds:

- `next` on every stage except a decision: a stage key, or `null` to end the run after this stage. Left out, the
  stage goes to the following stage in `stages` (version 1 behavior). A decision keeps `pass` (`"next"`, the
  following stage in the list, or a stage key) and `changes` (`goTo` an agent stage before it, `maxRevisions`).
- The run starts at the first stage in `stages` and moves one stage at a time; there are no parallel branches. A
  sign-off's Ask for changes goes back to the stage that wrote the file.
- `layout` (optional): `{"inputs": {"x", "y"}, "stages": {"<key>": {"x", "y"}}}` in points, finite numbers with
  |x| and |y| at most 100,000, at most one entry per stage (20). The plugin stores and returns it as is; the engine
  never reads it.
- "Before" means on every way from the first stage to this one: a `uses`, a check's `of`, a decision's `on` and a
  sign-off's `file` must come from a stage that always runs first.
- A run pins its revision, so editing a workflow never changes a running run.

Version 2 validation adds these issue codes: `unreachable_stage` (nothing leads to it), `cycle` (a loop that no
decision's `changes` made), `goto_not_earlier` (a decision's `goTo` can't lead back to the decision),
`next_unknown`, `uses_not_before` and `no_end` (no way ends the run). A draft can save with issues
(`valid: false`); only publishing needs it valid. A new workflow can start empty: `draft/save` with no
`workflowId`, `baseDraftVersion: 0` and `{schemaVersion: 2, name, description: "", roles: [], inputs: [],
limits: {stageMinutes: 20, maxRevisions: 2}, stages: []}` saves a draft with the issue `no_stages`.

Stage kinds (every stage has `key`, `kind`, `title`):

| Kind | Fields | What happens |
| --- | --- | --- |
| `agent` | `role`, `instructions` (8,000 characters), `tools` (toolset names), `uses` (references), `outputs` (`[{name, type, values?}]`, `values` only for `decision`), `minutes` (optional) | The role's agent runs one turn |
| `check` | `rules`: `{"type": "word_range", "of", "min", "max"}`, `{"type": "has_title", "of"}`, `{"type": "not_empty", "of"}`, `{"type": "number_range", "of", "min", "max"}` | The plugin checks earlier outputs. A failed rule fails the run at this stage |
| `decision` | `on` (a `decision` output with values `pass` and `changes`), `pass` (`"next"` or a later stage key), `changes`: `{"goTo": <earlier agent stage>, "maxRevisions": <0-5, optional>}` | `pass` goes on. `changes` sends the run back to `goTo` with the next iteration, and that stage's brief gets the review notes. One time too many and the run needs attention (`revision_limit`) |
| `signoff` | `file` (a `markdown_file` output) | The run waits for you. Approve goes on; Ask for changes sends the run back to the stage that wrote the file, with your notes |

`word_range` and `has_title` read a `markdown_file` or `text` output, `not_empty` also reads `notes`, and
`number_range` reads a `number`.

## For the app

`native-workflows-v1` in `/native/context` features. The plugin lists it on a POSIX computer with the Hermes
profile helpers, a Hermes CLI with `chat -q` (or `--query-file`) and a working store. Otherwise `/native/context`
says why in `unavailable`, with one fixed code: `{"unavailable": {"native-workflows-v1": "<code>"}}`, where the
code is `not_posix`, `profile_helpers_missing`, `chat_runner_missing` (update Hermes) or `store_unavailable`. The
`status` route's `runner.reason` uses the same codes.

`native-workflows-edit-v1` is listed together with `native-workflows-v1` when the plugin has everything marked
"version 2" here: the stage graph and layout, your templates, pin and unarchive. With only `native-workflows-v1`
the app shows a workflow's flow read-only.

`native-workflows-trigger-v1` (3.7.0) adds triggers, listed when this Hermes has cron jobs. Each workflow has one:
`{"kind": "manual"}` (someone, or an agent, starts each run) or `{"kind": "schedule", "schedule", "inputs",
"jobId"}`. `list` and `get` include it. `trigger/set` with `{workflowId, trigger: {kind, schedule?, inputs?}}`
saves it and returns `{trigger}`: a schedule is a cron expression (repeating, in this computer's time zone) and
needs a published, valid workflow and inputs it accepts. The plugin makes a Hermes cron job with no agent
(`no_agent`, delivered locally) whose script, `scripts/bighelp-workflow-<id>.py`, starts one run of the newest
version with the saved inputs (one per minute, however often the job fires). A new schedule replaces the job;
`manual` and `archive` remove it. Errors: `schedule_invalid`, `not_published`, `not_valid`, `inputs_invalid`,
`schedules_unavailable`.

Every route is `POST /api/plugins/loopdy/native/workflows/<path>` with the usual native headers: `If-Match` (the
context ETag) and `X-Loopdy-Request-ID` (a lowercase UUID). Responses echo the request ID and the ETag and are
`no-store`. Requests and responses are at most 196,608 bytes, and unknown request fields are refused. A mutation
sent again with the same request ID within 24 hours gets the first answer again.

Times are UTC, whole seconds, ending in `Z`. Optional fields marked `?` are left out when they don't apply. Apps
must ignore unknown fields and read unknown states and codes leniently.

Test vectors for every response are in `fixtures/contracts/workflows-v1/`. Each file is
`{"route", "request", "response"}`; `errors.json` lists every error code and `run-states.json` has a run in
every state. The app keeps byte-for-byte copies. Version 2 added files and changed none: `draft-save-new.json`,
`draft-save-v2.json` (a definition with `next`, `layout` and a decision loop), `validate-v2.json`, `get-v2.json`,
`list-v2.json`, `pin.json`, `unarchive.json`, `templates-save.json`, `templates-list-v2.json`,
`templates-use-yours.json`, `templates-delete.json`, `status-v2.json`, `runs-get-text-runner.json` and
`context-unavailable.json`.

| Path | Request | Response |
| --- | --- | --- |
| `status` | `{}` | `{coordinator: {state: "online"\|"starting"\|"offline", heartbeatAt, epoch}, slots: {used, total}, survivesAppClose: true, runner: {available, reason?, mode?: "stream"\|"text"}, hostName}` |
| `list` | `{includeArchived?}` | `{workflows: [{id, name, revision, draftVersion, hasDraft, stageCount, needsSetupRoles, valid, archived, lastRunAt, pinned}], waiting: [RunSummary], active: [RunSummary]}` (pinned workflows first; `active`: planned, working and needs-attention runs, newest first) |
| `get` | `{workflowId, revision?: <number> or "draft"}` (default `"draft"`) | `{workflow: {id, name, revision, latestRevision, draftVersion, archived, pinned, definition, bindings: [{role, agentId, approvedAt}]}, validation}` |
| `draft/save` | `{workflowId?, baseDraftVersion, definition}` (no `workflowId` and `baseDraftVersion: 0` makes a new workflow) | `{workflowId, draftVersion, validation}` |
| `validate` | `{workflowId}` | `{validation}` |
| `publish` | `{workflowId, draftVersion}` | `{workflowId, revision}` |
| `bind` | `{workflowId, role, agentId}` (`null` unbinds) | `{workflowId, bindings}` |
| `archive` | `{workflowId}` | `{workflowId, archived: true}` |
| `unarchive` (v2) | `{workflowId}` | `{workflowId, archived: false}` |
| `pin` (v2) | `{workflowId, pinned: true\|false}` | `{workflowId, pinned}` |
| `runs/start` | `{workflowId, revision, inputs, clientRunToken, sample: false}` | `{run: RunSummary}` |
| `runs/list` | `{workflowId?, filter: "all"\|"active"\|"for_you"\|"attention", before?, limit?}` (limit 1 to 50, default 20; `before` is the last response's `cursor`) | `{runs: [RunSummary], hasMore, cursor}` (newest first; `cursor` is null when there are no runs) |
| `runs/get` | `{runId}` | `{run: RunDetail}` |
| `runs/events` | `{runId, after, limit?}` (limit 1 to 200, default 100) | `{events: [{seq, at, kind, stageKey, attempt, text}], cursor, hasMore}` |
| `runs/control` | `{runId, action: "pause"\|"resume"\|"cancel"\|"retry", expectedVersion}` | `{run: RunSummary}` |
| `runs/signoff` | `{runId, stageKey, decision: "approve"\|"changes", artifactSha256, notes?}` (notes 2,000 characters) | `{run: RunSummary}` |
| `artifacts/read` | `{runId, sha256, offset, length}` (length 1 to 98,304) | `{sha256, offset, total, data, done}` (`data` is base64) |
| `templates/list` | `{}` | `{templates: [{id, name, description, source: "yours"\|"builtin", stageCount, roles, updatedAt}]}` (yours first, newest first; `updatedAt` is null for built-ins) |
| `templates/use` | `{templateId, name?}` | `{workflowId, draftVersion, validation}` (built-in or yours) |
| `templates/save` (v2) | `{workflowId, name, description?}` (name 1 to 80 characters, description up to 1,000) | `{templateId}`: a copy of the latest saved draft under that name, without who does each role. At most 100 |
| `templates/delete` (v2) | `{templateId}` | `{deleted: true}` (yours only) |

### Validation

`{valid, host, issues: [{stageKey?, code, message, severity: "error"|"warning"}]}`. `valid` is true when the
definition has no errors; only a valid draft can be published. `host` is true when it can also run on this
computer now: every role has an agent that exists and every tool is known. A run needs both.

Issue codes: `name_missing`, `no_stages`, `duplicate_key`, `role_unknown`, `role_unused` (warning), `use_unknown`, `use_forward`,
`tool_not_allowed`, `tool_terminal` (warning), `no_outputs`, `instructions_missing`, `choices_missing`,
`decision_source`, `decision_values`, `goto_invalid`, `pass_invalid`, `check_target`, `check_range`,
`signoff_file`, for schemaVersion 2 `unreachable_stage`, `cycle`, `goto_not_earlier`, `next_unknown`,
`uses_not_before`, `no_end`, and for this computer `role_unbound`, `agent_missing`, `toolset_unknown`,
`tool_scope_unsupported`.

### Runs

Run states: `planned` (waiting for a slot), `launched`, `running`, `checking_output`, `accepted` (a stage passed,
the next one is about to start), `waiting_for_you`, `needs_attention`, `succeeded`, `failed`, `cancelled`.

Stage states: `pending`, `launched`, `running`, `checking_output`, `accepted`, `waiting_for_you`,
`needs_attention`, `failed`, `cancelled`.

RunSummary: `{id, number, workflowId, workflowName, revision, state, stageKey, stageTitle, stageState, stagesDone,
stageCount, iteration, startedAt, updatedAt, endedAt, paused, attention?: {code, message}, failure?: {stageKey,
code, message}, waiting?: {kind: "signoff", stageKey, since}, version, sample}`. `number` counts runs of one
workflow. `version` goes up with every change; send it back as `expectedVersion`.

RunDetail adds `inputs`, `stages: [{key, kind, title, role, agentId, iteration, state, minutes, startedAt, endedAt,
attempts: [{id, number, iteration, state, agentId, launchedAt, endedAt, durationMs, tokens: {in, out},
outcomeCode}]}]`, `outputs: [{stageKey, iteration, name, type, sha256, bytes, wordCount?, value?}]` (the latest of
each output; `value` for numbers, decisions, notes and short text), `signoff?: {stageKey, artifact, previous?,
reviewNotes, history: [{iteration, decision, notes, artifactSha256, decidedAt}]}`, `tokens: {in, out}` and
`allowedActions`. `minutes` is the stage's time limit. Each stage also has `uses` (3.7.0): the references it
read, such as `inputs.topic` or `draft.draft`; a sign-off stage has `decisions: [{iteration, decision, notes,
decidedAt}]`, the person's sign-offs.

- Attention codes: `host_restarted`, `coordinator_restarted` (both: "We don't know how <stage> ended."),
  `revision_limit`, `agent_missing`.
- Attempt `tokens` is null when the runner can't count them (the text runner); the run's `tokens` adds up the
  attempts that could.
- Failure codes: `agent_exit`, `timed_out`, `spawn_failed`, `contract_no_block`, `contract_many_blocks`,
  `contract_bad_json`, `contract_missing_output`, `contract_wrong_type`, `contract_too_large`,
  `contract_bad_path`, `check_failed`, `storage_full`.
- Attempt states: `launched`, `running`, `checking_output`, `accepted`, `failed`, `timed_out`, `cancelled`,
  `unknown`.
- `allowedActions` is a subset of `cancel`, `retry`, `pause`, `resume`. Retry always starts a new attempt: of the
  failed stage, of the stage a failed check reads, or, after `revision_limit`, one more round from `goTo`.
- Cancel stops a running agent the same way as the time limit. Pause lets the running stage finish and then holds
  the run until resume (the app has no button for it yet).
- Sign-off: the app sends the SHA-256 of the file it showed. The plugin hashes its stored copy again and compares
  it with the file this stage was given; any difference is `409 approval_stale`. Approving never publishes or
  sends the file anywhere.

Event kinds: `run_planned`, `stage_launched`, `stage_running`, `stage_checking`, `stage_accepted`, `stage_failed`,
`check_passed`, `check_failed`, `decision_pass`, `decision_changes`, `waiting_for_you`, `approved`,
`changes_requested`, `needs_attention`, `adopted`, `retried`, `paused`, `resumed`, `cancel_requested`,
`cancelled`, `timed_out`, `succeeded`, `failed`, and `agent_error` (why a stage stopped before its turn began).
Event text is at most 200 characters.

### Errors

The usual native envelope: `{"error": {"code", "message", "retryable", "details": {}}}`.

| Status | Code | When |
| --- | --- | --- |
| 503 | `workflows_unavailable` | The feature isn't advertised |
| 503 | `runner_unavailable` | Hermes can't run stages right now |
| 503 | `store_unavailable` | The workflow store can't be opened |
| 404 | `workflow_not_found`, `revision_not_found`, `run_not_found`, `artifact_not_found`, `template_not_found`, `role_not_found`, `profile_not_found` | |
| 409 | `draft_conflict` | `baseDraftVersion` or `draftVersion` isn't the latest |
| 409 | `not_valid` | Publishing or starting something that isn't valid (for a start: on this computer too) |
| 409 | `workflow_archived` | Starting an archived workflow |
| 409 | `run_conflict` | `expectedVersion` isn't the run's version |
| 409 | `not_allowed` | The action isn't allowed in the run's state, deleting a built-in template, or a 101st template |
| 409 | `not_waiting` | Sign-off for a stage that isn't waiting |
| 409 | `approval_stale` | The file changed since the app showed it |
| 409 | `request_reused` | The request ID was used for another request |
| 413 | `definition_too_large` | The definition is over 64 KB |
| 422 | `invalid_definition` | The definition has the wrong shape or types |
| 422 | `inputs_invalid` | Missing or wrong run inputs |
| 422 | `sample_unsupported` | `sample: true` (not yet) |
| 429 | `queue_full` | 20 runs are already planned |

The general native codes (`context_required`, `context_changed`, `invalid_request`, `payload_too_large`,
`native_service_unavailable`, identity errors) apply too.

## For agents

The `bighelp_workflows` tool (toolset `bighelp_workflows`) gives agents the same workflows: `list`, `get`,
`templates`, `runs` and `run` read; `create`, `save_draft`, `start`, `control` and `archive` change drafts and
runs; `publish` (with role choices) and `set_trigger` with a schedule ask the person in Hermes' approval prompt
every time, and refuse when no one can answer. Sign-offs stay in the app. Inside a workflow stage the tool only
reads.
