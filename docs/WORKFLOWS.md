# Workflows

A workflow is a fixed list of steps (stages) that your Hermes agents work through one after another: for example
one agent researches, one writes a draft, one reviews it, and you approve the final file. The bighelp app builds
and starts workflows. The plugin runs them on the Hermes computer, so runs keep going when you close the app.

Nothing runs by itself. A run starts only when you tap Run, and a failed or interrupted stage never retries by
itself: the app shows what happened and offers Try again.

## How it runs

- Workflows are stored for the whole computer, not per agent, in `plugin-data/loopdy/workflows/` under the Hermes
  root. Each role in a workflow (Researcher, Writer, …) is bound to one agent (Hermes profile) on that computer.
- A small coordinator process runs the stages. The plugin starts it with `launchctl submit` (macOS) or
  `systemd-run --user` (Linux) when there is work, so it survives the dashboard and the app. It stops by itself
  after ten idle minutes. After a reboot it starts again the next time the gateway or dashboard starts, and a stage
  that was running then becomes "needs attention": nobody knows how it ended.
- Each agent stage runs one Hermes chat turn as that agent:
  `hermes -p <agent> --cli chat --source workflow --toolsets <tools> --query-file brief.md --format stream-json`,
  with `HERMES_QUIET_TURN_REPORT_FILE` set so Hermes writes how the turn ended. The chat's session source is
  `workflow`, so the app can keep these sessions out of its chat lists.
- At most two runs work at the same time (slots). Runs that wait for you use no slot. More runs wait as `planned`,
  oldest first, up to 20.
- A stage gets only the tools listed for it. A stage with no tools runs with only the to-do tool. Messaging,
  scheduled tasks, Kanban, delegation, Home Assistant, `clarify` and the all-in-one platform toolsets are never
  allowed, because a stage must not reach people or start other work. `terminal` is allowed with a warning.
- A stage has a time limit (20 minutes by default, at most 60). At the limit the plugin stops the agent (SIGTERM to
  its process group, then SIGKILL 30 seconds later) and the run fails at that stage.
- Storage limits: 50 MB of files per run, finished runs are removed after 30 days, at most 500 live lines per
  attempt.

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

## Definition (schemaVersion 1)

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

`native-workflows-v1` in `/native/context` features. The plugin lists it only on a POSIX computer with
`launchctl` or `systemd-run`, a Hermes that has the turn report (`HERMES_QUIET_TURN_REPORT_FILE`) and the chat
flags `--query-file` and `--format`, the Hermes profile helpers, and a working store. Otherwise the app says to
update Hermes or the plugin.

Every route is `POST /api/plugins/loopdy/native/workflows/<path>` with the usual native headers: `If-Match` (the
context ETag) and `X-Loopdy-Request-ID` (a lowercase UUID). Responses echo the request ID and the ETag and are
`no-store`. Requests and responses are at most 196,608 bytes, and unknown request fields are refused. A mutation
sent again with the same request ID within 24 hours gets the first answer again.

Times are UTC, whole seconds, ending in `Z`. Optional fields marked `?` are left out when they don't apply. Apps
must ignore unknown fields and read unknown states and codes leniently.

Test vectors for every response are in `fixtures/contracts/workflows-v1/`. Each file is
`{"route", "request", "response"}`; `errors.json` lists every error code and `run-states.json` has a run in
every state. The app keeps byte-for-byte copies.

| Path | Request | Response |
| --- | --- | --- |
| `status` | `{}` | `{coordinator: {state: "online"\|"starting"\|"offline", heartbeatAt, epoch}, slots: {used, total}, survivesAppClose: true, runner: {available, reason?}, hostName}` |
| `list` | `{includeArchived?}` | `{workflows: [{id, name, revision, draftVersion, hasDraft, stageCount, needsSetupRoles, valid, archived, lastRunAt}], waiting: [RunSummary], active: [RunSummary]}` (`active`: planned, working and needs-attention runs, newest first) |
| `get` | `{workflowId, revision?: <number> or "draft"}` (default `"draft"`) | `{workflow: {id, name, revision, latestRevision, draftVersion, archived, definition, bindings: [{role, agentId, approvedAt}]}, validation}` |
| `draft/save` | `{workflowId?, baseDraftVersion, definition}` (no `workflowId` and `baseDraftVersion: 0` makes a new workflow) | `{workflowId, draftVersion, validation}` |
| `validate` | `{workflowId}` | `{validation}` |
| `publish` | `{workflowId, draftVersion}` | `{workflowId, revision}` |
| `bind` | `{workflowId, role, agentId}` (`null` unbinds) | `{workflowId, bindings}` |
| `archive` | `{workflowId}` | `{workflowId, archived: true}` |
| `runs/start` | `{workflowId, revision, inputs, clientRunToken, sample: false}` | `{run: RunSummary}` |
| `runs/list` | `{workflowId?, filter: "all"\|"active"\|"for_you"\|"attention", before?, limit?}` (limit 1 to 50, default 20; `before` is the last response's `cursor`) | `{runs: [RunSummary], hasMore, cursor}` (newest first; `cursor` is null when there are no runs) |
| `runs/get` | `{runId}` | `{run: RunDetail}` |
| `runs/events` | `{runId, after, limit?}` (limit 1 to 200, default 100) | `{events: [{seq, at, kind, stageKey, attempt, text}], cursor, hasMore}` |
| `runs/control` | `{runId, action: "pause"\|"resume"\|"cancel"\|"retry", expectedVersion}` | `{run: RunSummary}` |
| `runs/signoff` | `{runId, stageKey, decision: "approve"\|"changes", artifactSha256, notes?}` (notes 2,000 characters) | `{run: RunSummary}` |
| `artifacts/read` | `{runId, sha256, offset, length}` (length 1 to 98,304) | `{sha256, offset, total, data, done}` (`data` is base64) |
| `templates/list` | `{}` | `{templates: [{id, name, description, stageCount, roles}]}` |
| `templates/use` | `{templateId, name?}` | `{workflowId, draftVersion, validation}` |

### Validation

`{valid, host, issues: [{stageKey?, code, message, severity: "error"|"warning"}]}`. `valid` is true when the
definition has no errors; only a valid draft can be published. `host` is true when it can also run on this
computer now: every role has an agent that exists and every tool is known. A run needs both.

Issue codes: `name_missing`, `no_stages`, `duplicate_key`, `role_unknown`, `role_unused` (warning), `use_unknown`, `use_forward`,
`tool_not_allowed`, `tool_terminal` (warning), `no_outputs`, `instructions_missing`, `choices_missing`,
`decision_source`, `decision_values`, `goto_invalid`, `pass_invalid`, `check_target`, `check_range`,
`signoff_file`, and for this computer `role_unbound`, `agent_missing`, `toolset_unknown`.

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
`allowedActions`. `minutes` is the stage's time limit.

- Attention codes: `host_restarted`, `coordinator_restarted` (both: "We don't know how <stage> ended."),
  `revision_limit`, `agent_missing`.
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
`cancelled`, `timed_out`, `succeeded`, `failed`. Event text is at most 200 characters.

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
| 409 | `not_allowed` | The action isn't allowed in the run's state |
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
