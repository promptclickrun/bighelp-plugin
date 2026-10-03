# Agent board: Feed, Ideas, Goals, Activity and Approvals history

The bighelp app shows each agent's board next to its chat. The plugin stores it per
profile in `<profile home>/plugin-data/loopdy/board.sqlite3`.

## Writing (agents only)

The `bighelp_board` tool (toolset `bighelp`) publishes Feed posts (`post`), Ideas
(`idea`) and Goals (`goal`, `update_goal`), and can `list` or `remove` items. Local
images are copied into `board-media/` at publish time; only those copies (PNG, JPEG,
GIF, WebP, HEIC, 8 MB max) are ever served. https image URLs are passed through.

Feed posts can also carry up to 10 files (`files`: absolute paths, 25 MB each). Unlike
images, files are kept as references, not copies: the board stores each path with its
name, type and size. Each path must pass Hermes' own delivery policy (the one `MEDIA:`
files in chat get: no credentials, system folders or Hermes' own secrets, and strict
hosts allow only their media folders) when the agent posts it, and again when the app
fetches it. Posting again with the same `id` and no `files` keeps a post's files;
`files: []` removes them.

Nothing creates posts on its own. The bundled `bighelp-feed-and-ideas` skill tells
agents to publish only what the user asked for, and to set up a scheduled job only
when the user wants something recurring.

## Recording (hooks)

- **Activity:** one row per completed turn that used at least one tool, with the
  user's request (first sentence), the reply's first sentence, the dominant tool
  category and the outcome. Subagent turns and plain chat are not recorded.
- **Approvals history:** `post_approval_response` decisions: Hermes' redacted
  command, its description and the choice.

## Native routes (`native-agent-board-v1`)

All take `agentId` and follow the native context/ETag/request-ID contract.

| Route | Body | Returns |
|---|---|---|
| `board/list` | `kinds`, `limit`, `includeDismissed` | `items` (goals with `category`) |
| `board/update` | `itemId`, `liked`, `dismissed`, `status` (goals); with feedback: `rating` (`up`/`down`/`none`), `reason` (thumbs down, 120 chars), `read` | `item` |
| `board/media` | `itemId`, `index` | `mimeType`, base64 `data` |
| `board/activity` | `limit` | `activity` with Hermes' session `title` |
| `board/approvals` | `limit` | `approvals` with `sessionTitle` |
| `board/identity` | — | `soul`, `memory`, `user`: `text` (64 KiB max), `updatedAt`, `truncated` |

With `native-agent-board-feedback-v1` (2.19.0):

| Route | Body | Returns |
|---|---|---|
| `board/read` | `itemIds` (up to 200), `read` | `updated` count |
| `board/promote` | `itemId` of an idea | the new goal `item`; the idea is hidden |

Items carry `rating`, `reason` and `read`. `liked` stays for older apps and maps
to thumbs up. Hiding (`dismissed`) is the app's delete and can be undone; hidden
items never reach the agent's `list`, which returns rating, reason and read so
the agent can post more of what the user rates up. Boards from before 2.19.0
keep their likes as thumbs up and start fully read.

With `native-agent-board-goal-categories-v1`:

Goals carry `category`: one of `health`, `relationships`, `finance`, `career`,
`interests`, `productivity` or `other`, or `""` for none. Agents set it with the
tool's `goal` and `update_goal` actions; anything outside the list is refused.
Republishing a goal without a category keeps the one it had. Feed posts and
ideas always have `""`. Making an idea a goal keeps its `section` as the
category when the section is one of those names. Goals from before this have no
category, and the app shows them under Other.

With `native-agent-board-files-v1`:

Feed posts carry `files`: `index`, `fileName`, `mimeType`, `byteCount` and `addedAt`
(when the agent attached it; attaching the same path again gives a new time, so the
app knows its saved copy is old) for each, never the path. The app gets a file through the attachment routes
([Agent attachments](AGENT_ATTACHMENTS.md)): `attachments/board` with `itemId` and
`index` returns an opaque attachment ID, and `attachments/fetch` downloads it in
chunks. Without the feature, posts have no files and the app shows Feed as before.

### Answers to ideas

Ideas also carry `answer`: `yes` (Let's do it), `goal` (Make it a goal), `not now`
(hidden) or `none`. The plugin records them without an app change:

- **Let's do it** sends the chat message `Yes, go ahead with this idea: “<title>”.`;
  the board's `pre_llm_call` observer marks the newest visible idea with that
  title `yes`.
- **Make it a goal** (`board/promote`) marks the idea `goal`.
- **Not now** and Delete (`board/update` with `dismissed: true`) mark an idea
  `not now`, unless it was already answered. Undo clears it. Hiding Feed posts
  and goals records nothing: deleting a read post is just clearing it.

The agent's `list` returns `answer` on each idea and an `answered` list: hidden
ideas answered in the last 30 days (up to 30). Publishing an idea with the id of
a `not now` from the last 30 days fails with a message telling the agent to offer
something else; after that, re-publishing brings it back with no answer. Ideas
hidden before this change have no answer.
