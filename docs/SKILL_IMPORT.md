# Skill import

Stock Hermes can make a skill from SKILL.md text, but it has no route that takes
a skill folder with its `scripts/`, `references/`, `templates/` and `assets/`.
This route lets the bighelp app add a skill to one agent from a `.zip` file, or
from one SKILL.md file. It is the app's Skills & Tools › Upload skill.

## How it works

The plugin checks the whole upload first and writes nothing until it passes.
Then it writes the skill into the skills folder of the agent's own profile with
the writers of Hermes (`tools.skill_manager_tool`). Hermes decides what a valid
skill is (name, header, description, sizes, paths). The plugin adds the rules
for archives and the security scan below.

- The skill's name comes from the `name:` line in the SKILL.md header.
- A skill with the same name in this agent is never replaced (`skill_exists`).
- If a step fails, or a security scan blocks the skill, the plugin deletes the
  skill it made and answers with an error. Nothing half-made stays.
- A skill whose name is a folder name that Hermes hides from its skill lists
  (`node_modules`, `venv`, `site-packages` and the others in
  `EXCLUDED_SKILL_DIRS`) is refused, as the skill could never be read back.
  The same holds for a category.
- After a good import the plugin clears the skills prompt cache of Hermes, so
  the agent's next chat lists the new skill.

### The security scan

An upload comes from outside, like a skill from the Skills Hub. So the plugin
scans every import, on every host:

1. When the last file is written, it runs `scan_skill` and
   `should_allow_install` of Hermes (`tools.skills_guard`) **once** on the whole
   skill, with the source name `bighelp-upload`. Hermes treats that as a
   community source, the same policy as a hub install: a `safe` verdict is
   allowed, and a `caution` or `dangerous` verdict (a high or critical finding)
   is blocked. Medium and low findings do not block. Hermes' scan also flags
   some files by their type or size, for example a `.bin` file, or a file over
   256 KB.
2. If the policy says block, the plugin deletes the skill and answers `blocked`.
   If the scan itself fails, the import fails and the skill is deleted too.
3. This scan does not depend on the `skills.guard_agent_created` setting. When
   that setting is on, Hermes' own scan also runs after each write, as it does
   for skills that agents make, and a block there answers `blocked` as well.

The answer carries the verdict (`scan`), so the app can show it.

### Rules for a .zip

- At most 3 MiB (before base64), and at most 200 files. Folder entries,
  `__MACOSX/` and `.DS_Store` are ignored and do not count.
- At most 16 MiB of file data together. A file is at most 1 MiB
  (`MAX_SKILL_FILE_BYTES`), and SKILL.md is at most 100,000 characters. The
  plugin checks the sizes in the archive and also limits what it reads.
- No absolute path, no `..`, no `\`, no `"`, no drive letter, no control
  character, no link or special file, and no password. A path is at most 240
  bytes.
- No path twice. Names that a disk treats as one file count as the same: case
  does not matter, and neither does the Unicode form (`é` as one character or
  as `e` and an accent).
- A path can't be both a file and a folder of another file (`assets/a` and
  `assets/a/b`).
- SKILL.md must be at the top of the archive, or inside exactly one top folder.
  That folder is removed.
- Every other file must be in `scripts/`, `references/`, `templates/` or
  `assets/`. A file anywhere else refuses the whole upload. A second SKILL.md
  refuses it too.
- Files are UTF-8 text. Only `assets/` may hold other (binary) files.

## Route

`POST /api/plugins/loopdy/native/skills/import`, feature
`native-skills-import-v1`, with the usual native headers (`If-Match` context
ETag and a lowercase `X-Loopdy-Request-ID`). It is listed in `/native/context`
only when the skill writers of Hermes import. The request body can be up to
4 MiB plus a small margin, more than the 192 KiB of the other routes.

Request (all fields required except `category`; no other fields):

```json
{"agentId": "nova", "fileName": "trip-notes.zip", "kind": "zip",
 "data": "<base64>", "category": "travel"}
```

| Field | Rule |
| --- | --- |
| `agentId` | A profile ID: `^[a-z0-9][a-z0-9_-]{0,63}$` |
| `fileName` | 1 to 200 characters. Only for the person; the plugin never uses it as a path |
| `kind` | `"zip"` or `"skillMd"` |
| `data` | Strict base64. For `zip` at most 3 MiB when decoded. For `skillMd` UTF-8 text of at most 100,000 characters |
| `category` | Optional. One folder name, as Hermes checks it. Empty means none |

Response (`200`):

```json
{"name": "trip-notes", "category": "travel",
 "files": ["SKILL.md", "assets/logo.png", "references/a.md"],
 "scan": {"verdict": "safe", "policy": "allow"}}
```

`files` are the relative paths written. SKILL.md is first, and the rest are in
alphabetical order. There are at most 200, each at most 240 bytes, so the
answer is under 50 KB. `category` is `null` when none was given. `scan.verdict`
is `safe`, `caution` or `dangerous`. `scan.policy` is `allow`, or `ask` when
Hermes' policy would ask: the import still goes ahead.

## Errors

The body is the usual `{"error": {"code", "message", "retryable", "details"}}`.
`message` is plain text for the person. For `invalid_skill` and `blocked` it
ends with the reason, because the app shows only the message. `details.reason`
holds the same reason alone, with host paths removed. A `blocked` answer also
has `details.findings`, up to 5 of `{severity, file, description}`. `details.path` names the file in the archive
that caused the error.

| Status | Code | Meaning |
| --- | --- | --- |
| 404 | `profile_not_found` | The agent no longer exists |
| 409 | `skill_exists` | This agent already has a skill with that name |
| 413 | `too_large` | A size limit above, or more than 200 files |
| 413 | `payload_too_large` | The request is bigger than the route takes |
| 422 | `invalid_request` | A field is wrong, or `data` is not base64 |
| 422 | `invalid_archive` | Not a `.zip`, damaged, password protected, an unsafe path, a link, or a file twice |
| 422 | `missing_skill_md` | No SKILL.md at the top or in one top folder |
| 422 | `unsupported_layout` | SKILL.md is too deep, there are more top folders, a file is outside the four folders, SKILL.md is repeated, or a file has the name of a folder |
| 422 | `binary_not_allowed` | A file that is not text, outside `assets/` |
| 422 | `invalid_skill` | Hermes does not accept the skill (header, name, category, empty text), or the name is one that Hermes hides |
| 422 | `blocked` | A security scan blocked the skill. The plugin deleted it |
| 428 | `context_required` | Load the native context first |
| 412 | `context_changed` | Before the write: refresh and retry. After the write, the message says to reconcile the outcome and `details` has `{name, category}`: the skill is added, and a retry would answer `skill_exists`. Read it back instead |
| 503 | `skill_import_unavailable` | The host does not list the feature |
| 503 | `skill_import_failed` | A write failed. The plugin deleted what it made. Retry is safe |
