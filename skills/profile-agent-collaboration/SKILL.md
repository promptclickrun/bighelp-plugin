---
name: profile-agent-collaboration
description: Use when handing work to a local teammate agent on this Hermes host.
---

# Reliable local team handoffs

## When to use

Use this skill for local task delegation to another agent profile on the same
Hermes host, checking a running handoff, or recovering a completed teammate
reply after a disconnect. This guide applies to local teammate profiles on this
host, not remote peer gateways.

## Why this runner exists

When a mobile client disconnects (for example when the user switches apps or
locks their phone), Hermes' WebSocket orphan reaper closes idle UI sessions
after a short grace window (`ws_orphan_reap`) and stops background processes
that were not started with `persist_on_release=true`. Hermes' built-in
`message_agent` background runner starts without `persist_on_release=true`, so
long-running teammate turns started through `message_agent` can be terminated
mid-turn before the reply returns.

For multi-minute teammate work, use `scripts/team_handoff.py` through the
`terminal` tool with `background=true`, `notify=true`, and
`persist_on_release=true` (or run a short synchronous `hermes -p <id> chat -Q`
command in the foreground when it will finish quickly).

## Running a durable handoff

1. Write the bounded brief to a private file (for example in `$TMPDIR`).
   Include the goal, the facts the teammate needs, and what to return. Never
   include secrets or private details the task does not need. The teammate must
   do the task and return its answer, never pass the task back or around.
2. Pick a unique job ID and start `scripts/team_handoff.py` with
   `background=true`, `notify=true`, and `persist_on_release=true`:

```text
terminal(
  command="python3 ~/.hermes/plugins/loopdy/skills/profile-agent-collaboration/scripts/team_handoff.py run --id <unique-job-id> --sender <your-profile-id> --target <teammate-profile-id> --message-file <brief-path> --workspace <workspace-dir> --timeout 3600",
  background=true,
  notify=true,
  persist_on_release=true
)
```

   If `HERMES_HOME` is not `~/.hermes` on this host, replace `~/.hermes` with
   this host's Hermes root directory (the parent of `plugins/loopdy`).
3. The helper writes an owner-only receipt under
   `<hermes-root>/state/team-handoffs/<job-id>/result.json`, runs the teammate's
   canonical `Bot Chat` with an isolated environment allowlist so sender session
   variables never leak into the target profile, and exits `0` only when the
   teammate produces a non-empty terminal result event.
4. Finish your turn after starting a background handoff. When the background
   process finishes, its completion notification wakes your session so you can
   relay the verified answer to the user. Never reply `[SILENT]` when the user
   is still waiting for the outcome.
5. If a disconnect interrupted the completion notification, inspect the receipt
   instead of re-sending the task:

```text
python3 ~/.hermes/plugins/loopdy/skills/profile-agent-collaboration/scripts/team_handoff.py status --id <unique-job-id>
```

   `status` checks process identity, distinguishes `orphan_running` from
   `interrupted_unknown`, and recovers a completed child result from
   `stdout.jsonl` if the wrapper exited early. Reusing the same `--id` with the
   same brief returns the saved receipt without running the task twice.

## Ownership rules

- The agent that asked a teammate still owns the user's final answer. A
  `queued` or `running` status is not completion.
- Verify any external file or state change a teammate reports before claiming
  it succeeded.
- If `status` reports `target_busy`, `failed`, `timed_out`, or
  `interrupted_unknown`, inspect partial work before retrying and tell the user
  plainly if a blocker remains.
