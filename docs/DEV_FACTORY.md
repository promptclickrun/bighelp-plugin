# Dev Factory

Dev Factory runs coding workflows on your git projects. A recipe lists one or more projects on the Hermes computer
and the steps to take in each: an agent changes the code, the tests run (an agent fixes what they find), you look at
the changes, and a pull request opens. When you start a recipe, the plugin makes one run per project. Each run walks
the steps on its own, so one project can wait for your sign-off while another one still runs its tests.

Dev Factory is part of [Workflows](WORKFLOWS.md). It uses the same coordinator, slots, attempts, sign-offs, events,
retries and storage. A recipe is a workflow definition with `"schemaVersion": 3`. Version 1 and 2 workflows work as
before.

Nothing runs by itself. A run starts only when you start it (or its schedule does), and no step retries by itself
except the fix agent of a command stage, up to the tries you set.

## How a run works

1. **Its folder.** Before its first stage, a run gets its own folder (`workspace`):
   - `worktree` (the default): a new git worktree with a new branch, made with
     `git -C <repo> worktree add -b factory/<recipe>-<run> <repo>/.worktrees/factory-<recipe>-<run> <base>`.
     `<recipe>` is the recipe's name in lowercase letters, digits and hyphens; `<run>` is 6 characters of the run's
     id. `.worktrees/` is where Hermes keeps worktrees too.
   - `local`: the project folder itself, on the branch it is on. Runs in `local` share the folder, so start one
     at a time per project.
   - The plugin never removes a worktree. Hermes' own worktree upkeep may remove one later, only when it is clean
     and its work is merged.
   - If the folder can't be made (no such branch, not a repository), the run needs attention with the reason, and
     Try again tries once more.
2. **Agent stages** work in the run's folder: it is their working folder and `TERMINAL_CWD`. The brief tells the
   agent which folder and branch it is in, that it may change files there, and not to push or open pull requests
   itself. The brief, the copies of earlier work (`inputs/`) and what the agent hands off (`out/`) stay in the
   attempt folder, and the brief names them by their full paths. Instructions can say `{project.label}` and
   `{project.key}`; the run's project fills them in. An agent stage with no `tools` gets `file`, `terminal` and
   `todo`, what a coding agent needs. `terminal` is allowed without a warning in version 3.
3. **Command stages** run a command in the run's folder, such as the project's tests:
   - The command is split like a shell would split it (`shlex.split`), but no shell runs it: no pipes, globs,
     variables or `&&`. Use a script in the project for more.
   - It gets an allowlisted environment: `PATH`, `HOME`, `SHELL`, the locale and time zone, `TMPDIR`, `USER`, and
     toolchain settings (`JAVA_HOME`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `GOPATH`, `GOROOT`, `CARGO_HOME`,
     `RUSTUP_HOME`, `DEVELOPER_DIR`, `NVM_DIR`, `PYENV_ROOT`, `VOLTA_HOME`, `PNPM_HOME`, `XDG_CACHE_HOME`,
     `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `SSH_AUTH_SOCK`) when Hermes has them, plus `CI=1`, `NO_COLOR=1` and
     `TERM=dumb`. Nothing else, so no AI provider keys.
   - It has a time limit (`minutes`, 20 by default, at most 60). At the limit it is stopped like an agent stage.
   - Its output (stdout and stderr together) is kept to its last 64 KB, with anything that looks like a key or a
     token hidden, as the attempt's `log` (a `text` output named `log`).
   - Exit code 0 passes and the run goes on.
   - Otherwise, while the stage has fix tries left, its `fix` role's agent gets a turn in the run's folder with the
     end of the output and the fix instructions, and then the command runs again. The fixer hands off `summary`
     (text): what it changed. Whatever the fixer does, the command decides.
   - Out of tries, or with no `fix`, the run waits for you with a reason, for example
     "Run the tests: still failing after 2 tries.". Try again runs the command again (with its fix tries back), and
     Cancel ends the run.
4. **A sign-off on the changes** (`"changes": true`) waits for you with a summary of what the run changed since it
   left its base branch: commits, uncommitted changes and new files (`.worktrees/` never counts). The plugin also
   hashes the folder's exact state (HEAD, `git status`, the diff and the new files' bytes). You approve with that
   hash; if the folder changed since, approving is refused (`approval_stale`) and you look again. Ask for changes
   sends the run back to the nearest agent stage before the sign-off (or the first stage), with your notes.
5. **A pull request stage** commits everything in the run's folder, pushes the branch and opens a pull request with
   the GitHub CLI:
   - `git add -A` (without `.worktrees/`), then `git commit -m <title>` when something is staged.
   - `git push -u origin <branch>`, then
     `gh pr create --base <base> --head <branch> --title <title> --body-file <body>` (`--draft` when asked).
   - The title is the stage's `titleFrom` output's first line, or the stage's title. The body is the rest of that
     output, or else the newest text or Markdown output of the run, with a line that says which workflow opened it.
   - It runs as its own short process (`python -I workflow_factory.py pull-request <attempt folder>`), so the
     coordinator keeps watching other runs meanwhile. It has 10 minutes; each git and gh call has its own timeout.
   - The run keeps `pull_request: {url, number}`. If the pull request is already open for the branch (a retry), it
     uses that one.
   - When it can't (no `gh`, `gh` not signed in, nothing to push, the push or GitHub refuses), the run waits for you
     with the reason. Nothing is committed until `gh` is known to be installed and signed in.
   - In `local` placement it refuses when the folder is on the base branch: "Pick a new worktree or a branch other
     than main to open a pull request."

## Definition (schemaVersion 3)

Version 3 is version 2 plus `projects`, `workspace`, command and pull request stages, and sign-offs on changes.

```json
{
  "schemaVersion": 3,
  "name": "Fix the check",
  "description": "An agent fixes it, the tests run, you review the changes and a pull request opens.",
  "roles": [{"key": "coder", "label": "Coder"}],
  "inputs": [{"key": "task", "label": "What to fix", "type": "long_text", "required": true}],
  "limits": {"stageMinutes": 30, "maxRevisions": 2},
  "projects": [
    {"key": "app", "label": "App", "path": "/Users/you/code/app"},
    {"key": "web", "label": "Web", "path": "/Users/you/code/web"}
  ],
  "workspace": {"placement": "worktree"},
  "stages": [
    {"key": "fix", "kind": "agent", "title": "Fix it", "role": "coder",
     "instructions": "Fix this in {project.label}. Hand off a pull request title and a summary.",
     "tools": [], "uses": ["inputs.task"],
     "outputs": [{"name": "title", "type": "text"}, {"name": "summary", "type": "markdown_file"}],
     "next": "tests"},
    {"key": "tests", "kind": "command", "title": "Run the tests",
     "run": {"*": "make test", "web": "npm test"}, "minutes": 20,
     "fix": {"role": "coder", "instructions": "The tests fail. Fix the code.", "maxTries": 2},
     "next": "review"},
    {"key": "review", "kind": "signoff", "title": "Review the changes", "changes": true, "next": "open_pr"},
    {"key": "open_pr", "kind": "pull_request", "title": "Open a pull request", "titleFrom": "fix.title",
     "draft": false, "next": null}
  ]
}
```

| Field | Rules |
| --- | --- |
| `projects` | Required in version 3. 0 to 10 items `{key, label, path}`; a run needs at least one (`no_projects`). `key` matches `^[a-z0-9][a-z0-9_-]{0,39}$` and is unique. `label` is 1 to 80 characters. `path` is the full, normalized path of a git repository's top folder on this computer (at most 1,024 characters) |
| `workspace` | `{"placement": "worktree" \| "local", "base"?: "<branch>"}`. Left out, it is `{"placement": "worktree"}` (and saved that way). `base` is the branch to start from; left out, the project's current branch. In `local`, `base` is only the pull request's base |
| command `run` | A command (1 to 2,000 characters), or `{"<project key>" \| "*": "<command>"}` (1 to 11 entries). A run uses its project's command, else `*` |
| command `minutes` | 1 to 60, default 20 |
| command `fix` | `{role, instructions, maxTries?}`: `maxTries` 0 to 3, default 2. The role must be bound like any other |
| sign-off `changes` | `true`, instead of `file`: a sign-off has exactly one of the two |
| pull request `titleFrom` | `<stage>.<output>`, a `text` or `markdown_file` output that always comes first |
| pull request `draft` | `true` opens a draft pull request |

Validation issue codes added in version 3: `no_projects`, `command_project` (a command for a key that isn't a
project), `command_missing` (a project has no command and there is no `*`), `pull_request_title`,
`pull_request_signoff` (a pull request stage must come after a sign-off on every way to it), and for this computer
`project_missing` (a project's folder isn't a git repository here). `role_unknown`, `instructions_missing` and
`role_unbound` also cover a command's `fix`.

### Templates

Three built-in templates, all `"schemaVersion": 3` with `"projects": []`, so a new draft from them has the issue
`no_projects` until the app fills in projects (and each project's command; the templates say `make test`):

| id | Name | Steps |
| --- | --- | --- |
| `fix-test-pr` | Fix, test, open a PR | agent (input `task`), command with a fix, sign-off on changes, pull request |
| `bump-dependency` | Bump a dependency everywhere | agent (inputs `package`, `version`), command with a fix, sign-off on changes, pull request |
| `test-sweep` | Nightly test sweep | one command, no fix, `local` placement. A failing project waits for you; no AI runs |

## For the app

`native-workflows-code-v1` in `/native/context` features: listed with `native-workflows-v1` when `git` is on this
computer's `PATH`. Without git it isn't listed (the startup log says why). The GitHub CLI is checked only when a
pull request stage runs.

Routes are the [Workflows routes](WORKFLOWS.md#for-the-app). Version 3 adds:

| Path | Request | Response |
| --- | --- | --- |
| `templates/list` | `{includeCode?: true}` | Dev Factory templates (and your templates made from version 3 workflows) are listed only with `includeCode: true`, so older apps never see them |
| `runs/start` | adds `projects?: ["<key>", …]` (1 to 10, some of the recipe's projects; all when left out) | adds `group_id` and `runs: [{run_id, project}]`. `run` is the first run's summary. Each run is `planned` and waits for a slot like any run |

```json
{"route": "workflows/runs/start",
 "request": {"workflowId": "wf_6e5d4c3b2a190807", "revision": 1, "inputs": {"task": "Fix it"},
             "clientRunToken": "4a3b2c1d-0e9f-4a8b-9c7d-6e5f4a3b2c1d", "projects": ["app"]},
 "response": {"run": {"id": "run_164e6fa2b3c4d5e6", "state": "planned", "…": "…",
                      "group_id": "grp_ca29965cc4f52207",
                      "project": {"key": "app", "label": "App", "path": "/Users/you/code/app"},
                      "workspace": null, "pull_request": null, "reason": null},
              "group_id": "grp_ca29965cc4f52207",
              "runs": [{"run_id": "run_164e6fa2b3c4d5e6", "project": "app"}]}}
```

A start sent again with the same `clientRunToken` returns the same runs. `projects` on an older workflow, or a key
the recipe doesn't have, is `422 projects_invalid`.

### Runs

A version 3 run's `RunSummary` (in `runs/list`, `runs/get`, `list` and every route that returns a run) adds:

- `group_id`: the start it came from.
- `project`: `{key, label, path}`.
- `workspace`: `{placement, path, branch, base}`, or `null` until its folder is ready.
- `pull_request`: `{url, number}`, or `null`.
- `reason`: why it waits for you, needs attention or failed, as one sentence the app can show; `null` otherwise.
  For example "Look at the changes in App and approve them.", "Run the tests: still failing after 2 tries." or
  "Install the GitHub CLI (gh) on this computer to open pull requests.".
- `waiting.kind`: `signoff`, `command` or `pull_request`. A run waiting on a command or a pull request has
  `retry` and `cancel` in `allowedActions`.

Version 1 and 2 runs have none of these fields.

Each stage's state is in `runs/get` as before: `stages: [{key, kind, title, state, attempts, …}]` with the states in
[Workflows](WORKFLOWS.md#runs) (`pending`, `running`, `accepted` for done, `waiting_for_you`, `failed`, …). Version 3
adds to `RunDetail`:

- A command stage's attempts are its command runs and the fixer's turns, with `kind`: `"command"` or `"fix"`. A
  command attempt also has `exit_code` (null when stopped or unknown), `tries` (fix attempts used before it) and
  `log: {sha256, bytes}` (read it with `artifacts/read`). Its stage has `minutes`.
- `outputs` has `<command stage>.log` (its newest output) and `<command stage>.summary` (the newest fixer's summary).
- A sign-off on changes has `changes: {files, additions, deletions, branch, base}` in its stage entry, and while it
  waits, `signoff: {stageKey, changes, sha256, reviewNotes, history}` (no `artifact`). Approve or ask for changes with
  `runs/signoff` and `artifactSha256` set to `signoff.sha256`.

```json
{"attempts": [
  {"id": "att_bec78ff3757533c0", "number": 1, "kind": "command", "state": "failed", "outcomeCode": "command_failed",
   "exit_code": 1, "tries": 0, "log": {"sha256": "24de7a65ed3770f3b25a7e84504dfeeda5ba57eabb8d5193ff3c224f9773e1b9",
   "bytes": 27}, "agentId": "", "…": "…"},
  {"id": "att_f1a5473286cf4c54", "number": 2, "kind": "fix", "state": "accepted", "agentId": "writer", "…": "…"}]}
```

Attention codes added: `workspace_failed` (its folder couldn't be made) and `changes_unreadable`. Attempt outcome
codes added: `command_failed`, `fix_failed`, and the pull request step's result codes (`gh_missing`,
`gh_signed_out`, `not_on_branch`, `same_branch`, `no_changes`, `commit_identity`, `commit_failed`, `push_failed`,
`create_failed`, `step_failed`). Event kinds added: `workspace_ready`, `command_passed`, `command_failed`,
`fix_done` and `pull_request_opened`.

Test vectors: `fixtures/contracts/workflows-v1/templates-list-code.json`, `draft-save-code.json`,
`runs-start-code.json`, `runs-get-code-command.json` (waiting on a command, after one fix),
`runs-get-code-review.json` (waiting on your review of the changes), `runs-signoff-code.json` and
`runs-get-code-done.json` (with its pull request).

### Errors

| Status | Code | When |
| --- | --- | --- |
| 422 | `projects_invalid` | `projects` on a workflow without projects, an unknown or repeated key, or an empty list |

## For agents

`bighelp_workflows` builds and runs recipes like any workflow: `templates` lists the Dev Factory templates, `create`
and `save_draft` take version 3 definitions, and `start` takes `projects`. Publishing still asks the person.

## Safety and privacy

- Commands and git run with fixed argument lists, never a shell, and each has a timeout.
- A command stage's environment is an allowlist (above); Hermes' and the AI providers' keys never reach it. Its
  log hides anything shaped like a key or token before it is stored.
- Preparing a worktree and reading changes use the person's own git setup (hooks, LFS), like a terminal would, with
  prompts turned off.
- The pull request step uses the person's own git and GitHub CLI sign-ins: it gets `PATH`, `HOME`, `SSH_AUTH_SOCK`,
  `XDG_CONFIG_HOME`, the locale, and `GH_TOKEN`, `GITHUB_TOKEN` and `GH_HOST` when the coordinator has them. The
  coordinator starts with a scrubbed environment, and tokens are never handed to it on a command line (they would
  show in the process list), so in practice `gh` uses the sign-in it stored with `gh auth login`. Its output stays in
  the attempt folder; the app gets only fixed sentences, with git's or GitHub's last line where it helps, keys hidden.
- `.worktrees/` is never part of a run's changes or commits.
- Runs show their project's path and their folder: the app sent those paths when it made the recipe.
