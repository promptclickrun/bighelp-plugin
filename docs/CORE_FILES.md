# Core files

The Core Files page of a bot in the bighelp app shows the files that tell the agent who it is and what it knows.
The person can read and edit them:

| Kind | File | What it holds |
| --- | --- | --- |
| `soul` | `SOUL.md` in the Hermes home of the agent | Who the agent is |
| `user` | `memories/USER.md` | What the agent remembers about the person |
| `memory` | `memories/MEMORY.md` | The notes of the agent |
| `agents` | The `AGENTS.md` that Hermes loads for a chat | The rules of the chat's project |

The route takes a kind, not a path, so it cannot reach other files. It shows `agents` only when there is an
`AGENTS.md`, and it saves `AGENTS.md` only where it exists. The other three files can be new.

## Which AGENTS.md

Hermes loads `AGENTS.md` from the working folder of the chat: the chain from the git root down to that folder,
where the deepest file wins. The route shows that deepest `AGENTS.md`. When the app sends `sessionId`, the route
reads the folder of that chat from the state of the agent. For a compressed chat, it reads the live part. When a
chat moves to another project, its folder changes, so the file changes with it. The `project` field is the name of
the Hermes project that holds the folder, or else the name of the folder.

Without `sessionId` (apps before `native-core-files-chat-v1`), the route shows the `AGENTS.md` in the main folder
of the active project of the agent.

## How it works

- A read sends the whole file and its SHA-256. A file longer than 512 KB comes truncated, and the app opens it
  read-only.
- A save sends the whole text and the hash that the app read (`null` when no file was there). When the file
  changed since then, the route does not save, and answers `409 file_changed`.
- A save writes a temporary file next to the file and replaces the file in one step.
- Hermes' memory tool writes `USER.md` and `MEMORY.md` under a lock next to them. A save of these files takes the
  same lock, so a save and a memory write never mix.
- The route refuses links (`422 invalid_path`).
- Hermes reads these files when a chat starts. A change reaches the next chat of the agent.

## Routes

All three are `POST /api/plugins/loopdy/native/core-files/…`, feature `native-core-files-v1`, with the usual native
headers (`If-Match` context ETag and a lowercase `X-Loopdy-Request-ID`).

Each request can add `"sessionId": "…"`, the stored ID of the chat whose `AGENTS.md` to use.

`list`, request `{"profile": "nova", "sessionId": "local-…"}`:

```json
{"files": [{"kind": "soul", "name": "SOUL.md", "exists": true, "size": 412, "updatedAt": 1790000000},
           {"kind": "user", "name": "USER.md", "exists": true, "size": 96, "updatedAt": 1790000000},
           {"kind": "memory", "name": "MEMORY.md", "exists": false, "size": 0},
           {"kind": "agents", "name": "AGENTS.md", "exists": true, "size": 2048, "updatedAt": 1790000000,
            "project": "Garden"}]}
```

`read`, request `{"profile": "nova", "kind": "soul"}`: the `list` row, and `text`, `truncated` and `sha256`
(`null` for a file that is not there).

`write`, request `{"profile": "nova", "kind": "soul", "text": "…", "expectedSha256": "…"}`: the new `read`
answer.

Errors:

- `404 file_not_found`: no `AGENTS.md`, or an unknown agent.
- `409 file_changed` and `413 file_too_large`.
- `422`: a bad body, a link or an unknown kind.
- `412 context_changed`, and `503 core_files_unavailable` when the feature is not in the list.

## Feature

The plugin lists `native-core-files-v1` and `native-core-files-chat-v1` when the profile helpers of Hermes import.
`native-core-files-chat-v1` tells the app that the routes take `sessionId`. Older plugins refuse the field. Both
features are in `LATE_FEATURES`.
