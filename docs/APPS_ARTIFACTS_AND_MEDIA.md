# Apps tab: Artifacts and Media

The bighelp app's Apps tab shows what an agent made. Both routes follow the
native context/ETag/request-ID contract and never return host paths the phone
could not already see.

## Artifacts (`native-workspace-recent-v1`)

`POST workspace-files/recent` with `{"path": null}` returns the same listing
shape as `workspace-files/list` for the agent's working folder, newest first
(at most 300 rows):

- Files the serving profile's agent created or edited (`write_file`, `patch`)
  or delivered (`MEDIA:`), ranked by when it wrote them. Paths come from the
  profile's own `state.db`, must still exist, and are re-opened `O_NOFOLLOW`
  below the root, so history can never point outside the workspace.
- Plus files in the top two folder levels, ranked by creation time. Hidden
  entries, dependency/build folders, bundles, housekeeping files (logs, locks)
  and code repositories (folders with `.git`) are skipped.

A workspace can hold millions of files, so this never walks the whole tree. It
answers in about a second on a large Mac workspace.

## Which folder

`workspace-files/scope`, `list`, `read` and `recent` all start from the serving
profile's working folder, found the way Hermes finds it (`workspace_root.py`):

- `terminal.cwd` when it names a folder. `~` is the user's home, and a relative
  path is relative to the folder Hermes was started in, as in Hermes.
- Unset, or `.`, `auto` or `cwd`: the folder Hermes starts a new chat in. The
  plugin asks Hermes' chat gateway (`config.get` with key `project`, as Hermes
  Desktop does) and otherwise applies the same rule: `TERMINAL_CWD`, else the
  folder Hermes was started in.

Every response's `workspace` says `"source": "terminal.cwd"` and an `origin`:
`config`, `default` (Hermes chose it) or `docker-volume`.

The plugin only reads files on its own computer, and never inside Hermes' own
folders (its home and every profile's), which hold its settings and keys. Those
are left out of listings and refused for reads. When it can't share an agent's
files, the error code says why:

| Code | Status | When |
|---|---|---|
| `workspace_in_container` | 409 | The terminal backend is a container (docker, singularity, modal, daytona, vercel_sandbox). Docker is served when Hermes bind-mounts a host folder as the working folder (`docker_mount_cwd_to_workspace`, or a `docker_volumes` entry holding it) |
| `workspace_on_remote` | 409 | The terminal backend is `ssh`: the files are on the other computer |
| `workspace_windows_unsupported` | 501 | The host runs Windows. The confined traversal needs descriptor-relative opens with `O_NOFOLLOW`, which Windows doesn't have |
| `workspace_not_configured` | 409 | No `terminal.cwd`, and Hermes' own choice is the disk root, Hermes' own folders or its install |
| `workspace_hermes_folder` | 409 | `terminal.cwd` is Hermes' own folder |
| `workspace_unavailable` | 409 | `terminal.cwd` names a folder that is missing or can't be opened |

## Media (`native-agent-media-v1`)

`POST attachments/recent` with `{"agentId", "limit"}` (1-36) returns
`items`: `id`, `fileName`, `mimeType`, `byteCount`, `storedId`, `createdAt`.
They are the newest pictures and videos the agent delivered with `MEDIA:` or
made with `image_generate`/`video_generate`. Each goes through the gateway's
delivery policy and the same attachment store as chat attachments; bytes are
fetched with `attachments/fetch`.
