# Big files from the phone

People send their agent files from bighelp: a video, a large PDF, an archive. Hermes' own `file.attach` takes
a file's bytes in one WebSocket frame, so the app uses it only for files up to 8 MiB. Bigger files, up to
256 MiB, come through this plugin in pieces. A dropped connection loses at most the piece that was on its way,
and the phone can send the pieces from the background.

Feature: `native-uploads-v1` in `/native/context`. Without it, the app keeps the 8 MiB limit and says to update
the plugin.

## How a file reaches the agent

1. The app asks to `start` an upload with the file's name, type, size and SHA-256.
2. It sends each piece to `chunk`, in any order, more than one at a time if it likes. The host keeps which
   pieces it has; `status` says which.
3. `finish` checks the whole file against its SHA-256 and moves it into the agent's working folder, the one
   its Files tab shows (`workspace_root`), as `uploads/<date>/<name>`: `Trip.mov`, then `Trip-2.mov` for a
   second file of that name. It answers with that path. The chat links the file there, so it opens in the
   agent's Files, and no sweep here ever removes it.
4. The app gives that path to Hermes' own `file.attach` (`{"session_id", "path", "name"}`). A file inside
   the chat's working folder is used where it is; otherwise Hermes copies it into the agent's attachments,
   the way it does for every attached file (its sandbox mounts and `attachments.storage` included). It
   answers with an `@file:` reference for the message.
5. The app calls `discard`, which removes the upload's record.

An agent without a working folder on this computer (a container or SSH backend), or one whose `uploads`
folder isn't a plain folder (the plugin never follows a link there), keeps the file staged in the plugin's
own folder instead. `finish` then answers `"inWorkspace": false`, and Hermes' `file.attach` copies it in.

Hermes does not put a binary file's bytes into the model's context. For `@file:` it tells the agent the file's
type, size and where it is on disk, so the agent works with it through its own tools (for a video, `ffprobe`
or `ffmpeg`).

## Routes

All are `POST /api/plugins/loopdy/native/uploads/<operation>` with `If-Match` (the context ETag) and
`X-Loopdy-Request-ID`, like every native route. Answers are JSON, `no-store`, and at most 16 KB.

### `start`

```json
{"agentId": "default", "fileName": "Trip.mov", "mimeType": "video/quicktime",
 "byteCount": 83886080, "sha256": "<64 lowercase hex>"}
```

`byteCount` is 1 to 268,435,456. The name becomes one path part with no control characters and no leading dot,
at most 180 bytes. The same person starting the same file again (agent, name, size and SHA-256) gets the
unfinished upload back, with the pieces the host has.

The answer, also for `status` and `chunk`:

```json
{"uploadId": "<32 lowercase hex>", "fileName": "Trip.mov", "mimeType": "video/quicktime",
 "byteCount": 83886080, "chunkBytes": 4194304, "receivedChunks": [0, 1, 2], "receivedBytes": 12582912,
 "finished": false, "expiresAt": 1798761600.0}
```

### `chunk`

The body is the piece itself, `Content-Type: application/octet-stream`, with an exact `Content-Length`.
Headers:

- `X-Loopdy-Upload-ID`: the upload.
- `X-Loopdy-Upload-Offset`: where the piece starts, a multiple of `chunkBytes`.
- `X-Loopdy-Chunk-SHA256`: the piece's SHA-256. A piece without it, or one that doesn't match, is refused.

Each piece is `chunkBytes` long, except the last one. The host records a piece only after all of it arrived and
was written to disk, so a piece cut off by a drop is simply sent again.

### `status`, `finish`, `discard`

`{"uploadId": "<32 lowercase hex>"}`. `finish` adds `"path"`, where the file now is, and `"inWorkspace"`
(see above). Calling `finish` or `discard` again is safe.

This is the one plugin answer with a host path, an exception to the rule in AGENTS.md that the maintainer
approved on 2026-10-31. The app shows a file in the chat as a line with its name that opens it in the
agent's Files, never the path as text. The reasons:

- Hermes' `file.attach` takes a host path or the file's bytes in one frame. Bytes are what this route avoids.
- The plugin can't attach a file for a chat session itself. Only Hermes' gateway holds the session.
- `file.attach` answers the app with the attached file's absolute path and `@file:` reference anyway.
- Hermes' own upload route (`/api/chat/file-upload`) answers with its staged path the same way.

## Errors

| Status | Code | When |
| --- | --- | --- |
| 404 | `upload_not_found` | No such upload, it expired, or it belongs to someone else. Start again. |
| 404 | `profile_not_found` | The agent no longer exists. |
| 409 | `upload_incomplete` | `finish` before every piece arrived. |
| 409 | `upload_finished` | A piece for an upload that is finished. |
| 413 | `payload_too_large` | A piece bigger than `chunkBytes`. |
| 422 | `upload_offset_invalid`, `upload_length_invalid` | A piece that isn't one whole piece of this file. |
| 422 | `upload_chunk_corrupt` | A piece that doesn't match its SHA-256. |
| 422 | `upload_corrupt` | The pieces don't add up to the file's SHA-256. The host forgets the pieces; send them again. |
| 408 | `upload_chunk_timeout` | A piece's body took over 2 minutes to arrive. Send it again. |
| 429 | `uploads_busy` | The host already holds 8 uploads (3 for one person), or 1 GiB of them. |
| 503 | `uploads_unavailable` | The host can't stage files. |
| 409 | `upload_no_space` | Not enough free disk: twice the file, plus what started uploads still need, plus 512 MiB. Also when the disk fills while a piece or the record is written. |

## Limits and cleanup

- 256 MiB a file, pieces of 4 MiB, 8 uploads (3 for one person) and 1 GiB at once for each host.
- The host checks free space before it accepts a file: room for the staged copy, Hermes' copy into the
  attachments, what uploads already started still need (staged files are sparse until their pieces
  arrive), and 512 MiB more.
- Pieces are staged in `plugin-data/loopdy/uploads/<uploadId>/`, owner-only (`0700` folders, `0600` files).
  A finished file keeps `0600` in the agent's folder. Moving it on the same disk takes no room. Across
  disks, the plugin first checks the other disk has room for the file plus 512 MiB.
  The phone never names a path. Symbolic links there are never followed, and are removed.
- An upload idle for a day is removed, finished or not: its record, and its staged copy if it has one. A
  file moved into the agent's working folder is never removed here. Any upload call sweeps them, at most once a minute,
  and a call that finds one expired removes it at once.
- A piece's body must arrive within 2 minutes, so a stalled phone frees its memory slot.
- At most 4 pieces are held in memory at once in each process.
