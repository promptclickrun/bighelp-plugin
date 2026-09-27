# Agent attachments

When an agent sends a file, picture or video (with a `MEDIA:<path>` line, or from an image or video generation
tool), bighelp shows it in the chat as a picture, video or file card. The phone never sees or names a host path.

## How it works

All routes are `POST /api/plugins/loopdy/native/attachments/<operation>` with the usual native session checks
(see [Native workspace API](NATIVE_WORKSPACE_API.md)). The plugin advertises `native-agent-attachments-v1`.

- **`resolve`**: the app sends up to 50 assistant messages from one chat (`agentId`, `storedId`, and each
  message's text). For each `MEDIA:` path, the plugin applies Hermes' own delivery rules (the same ones every
  messaging platform uses, including `gateway.media_delivery_allow_dirs` and the other `gateway.*` media
  settings). It also requires that an assistant message in that chat, or in the chat it was compacted from,
  really sent that path. Approved files get an opaque ID, a file name, a type and a size, and the `MEDIA:` line is
  removed from the displayed text.
- **`fetch`**: the app downloads an attachment by ID in chunks of about 3 MB, up to 25 MB per file.
- **`recent`**: the pictures and videos an agent sent or generated lately, for the Apps tab (see
  [Artifacts and media](APPS_ARTIFACTS_AND_MEDIA.md)). Advertised as `native-agent-media-v1`.

Approved files are copied into a per-profile cache
(`<Hermes profile home>/plugin-data/loopdy/agent-attachments.sqlite3`), so reopening an old chat still works if
the original file has moved. Missing, denied or oversized files simply show no attachment.
