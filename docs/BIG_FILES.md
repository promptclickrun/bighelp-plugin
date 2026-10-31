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
3. `finish` checks the whole file against its SHA-256 and answers with the path of the staged copy.
4. The app gives that path to Hermes' own `file.attach` (`{"session_id", "path", "name"}`). Hermes copies the
   file into the agent's attachments, the way it does for every attached file (its sandbox mounts and
   `attachments.storage` included), and answers with an `@file:` reference for the message.
5. The app calls `discard`, which removes the staged copy.

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
- `X-Loopdy-Chunk-SHA256` (optional): the piece's SHA-256. A piece that doesn't match is refused.

Each piece is `chunkBytes` long, except the last one. The host records a piece only after all of it arrived and
was written to disk, so a piece cut off by a drop is simply sent again.

### `status`, `finish`, `discard`

`{"uploadId": "<32 lowercase hex>"}`. `finish` adds `"path"`, the staged file for `file.attach`. It is the
only answer with a host path, because `file.attach` needs one; Hermes' own upload route
(`/api/chat/file-upload`) answers the same way. Calling `finish` or `discard` again is safe.

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
| 429 | `uploads_busy` | The host already holds 8 uploads, or 1 GiB of them. |
| 503 | `uploads_unavailable` | The host can't stage files. |
| 507 | `upload_no_space` | Less free disk than twice the file plus 512 MiB. |

## Limits and cleanup

- 256 MiB a file, pieces of 4 MiB, 8 uploads and 1 GiB at once for each host.
- The host checks free space before it accepts a file: room for the staged copy, Hermes' copy into the
  attachments, and 512 MiB more.
- Staged files live in `plugin-data/loopdy/uploads/<uploadId>/`, owner-only (`0700` folders, `0600` files).
  The phone never names a path. Symbolic links there are never followed, and are removed.
- An upload idle for a day is removed, finished or not.
- At most 4 pieces are held in memory at once in each process.
