# HTML apps: interactive pages in the chat

An agent can make a small interactive app: a tool, a planner, a picker, a live dashboard. The chat shows it
as a card with a title, a short description and **Open App**. Open App loads the page in a full-screen web
view on the phone. The app can show live data. When the agent allows it, the app can also send the person's
answer back to the agent as the person's next chat message, the way a form answer does.

The plugin stores and serves the page. It does not check what the page loads: no content security policy,
and no limits on sites or libraries. The page is the agent's own code, running in the phone's web view.

Feature: `native-html-apps-v1` (listed with the other features, and last for apps that don't ask for every
feature). A chat says its app can draw the card by adding `html_app` to `renders` in its `people/speaking`
note, like the other card components in [Cards](CARDS.md).

## The `html_app` card element

Only `bighelp_show_app` makes one. `bighelp_render_card` and card templates refuse it (`apps_tool_only`), and
the card tool's parameters don't list it. It is the root and only element of its card, a leaf with no
children, and its props are exactly these four:

| Prop | Value |
| --- | --- |
| `app_id` | `a_` and 32 lowercase hex characters |
| `description` | 1 to 240 characters, single spaces, no control characters |
| `sends_answers` | true or false |
| `live` | true when the app has data (`data` or `data_path` was given) |

The card the tool renders:

```json
{"schema": "loopdy.card", "version": 1, "title": "Trip planner",
 "spoken_summary": "An app to open in bighelp: Trip planner.", "data_sources": [], "root": "app",
 "elements": {"app": {"type": "html_app", "children": [],
   "props": {"app_id": "a_0123456789abcdef0123456789abcdef", "description": "Plan the stops of a road trip.",
             "sends_answers": true, "live": false}}}}
```

The title is 1 to 60 characters. A notification for a reply with this card says `<title>: <description>`.
The shared example is `fixtures/loopdy_card_v1/static-html-app.json`.

## Tools

Both are in the `bighelp` toolset. Errors come back as `{"success": false, "error": <code>, "message": <words>}`.

### `bighelp_show_app`

| Argument | |
| --- | --- |
| `title` | Required. 1 to 60 characters. |
| `description` | Required. 1 to 240 characters, shown on the card. |
| `html` or `path` | Exactly one. `html` is the page. `path` is an `.html` or `.htm` file the agent wrote in its working folder. |
| `data` or `data_path` | At most one. `data` is any JSON. `data_path` is a JSON file in the working folder. |
| `sends_answers` | Optional, default false. |
| `app_id` | Optional. An app of this agent to replace. |

- `path` and `data_path` are read the way the Files routes read a file: the agent's own working folder only
  (a relative path starts there), no links, never Hermes' own folders or credential files. A `data_path` file
  that isn't there yet is allowed (a script or scheduled task can write it later); the note says so.
- Limits: the page is at most 16,777,216 bytes (16 MiB) of UTF-8 text. Inline data is at most 1,048,576 bytes as compact
  JSON. A `data_path` file is read up to 1,048,576 bytes.
- With `app_id`, the app's page, title, description, `sends_answers` and data source are replaced (without
  `data` or `data_path`, the app has no data) and its `revision` goes up by one. It keeps its id.
- It returns the usual card delivery (`schema`, `version`, `card`, `display_markdown`, `instruction`) plus
  `app_id`, `revision` and a `note`. The note says the person opens the app from the card and, when
  `sends_answers` is true, that an answer arrives as the person's next message.

| Code | When |
| --- | --- |
| `apps_unavailable` | The call isn't in a bighelp chat with the person, or the chat's app didn't list `html_app` in `renders` ("This chat's bighelp app can't show apps. Ask the person to update bighelp.") |
| `invalid_arguments` | Unknown or missing arguments, both or neither of `html` and `path`, both `data` and `data_path`, a bad `title`, `description`, `sends_answers` or `app_id`, a page that isn't UTF-8 or is empty |
| `invalid_path` | A `path` or `data_path` that is outside the working folder, a link, or a file that can't be read |
| `html_too_large`, `data_too_large` | Over the limits above |
| `app_not_found` | An `app_id` that isn't one of this agent's apps |

### `bighelp_update_app_data`

`app_id` and `data` are both required. `data` is any JSON, within the inline limit. `null` clears it. It
returns `{"app_id", "updated_at", "note"}`; the note says the open app gets the new data within a few seconds
and the card isn't posted again. It needs no bighelp chat, but the app must belong to this agent. An app that
reads `data_path` refuses (`data_from_file`: "This app reads its data from <path>. Write that file instead.").

### What the agent is told

The tool description, the chat brief and the `bighelp` skill say when an app fits (something interactive; a
plain card or form is better for a simple answer), that the page is one self-contained page that may load any
scripts, styles or APIs, that it should follow `prefers-color-scheme` and fit a phone, and the page API:

- `await window.bighelp.getData()` returns the latest data, or null.
- `window.bighelp.onData(fn)` calls `fn(data)` once at start when there is data, and again whenever it
  changes while the app is open. It returns an unsubscribe function.
- `await window.bighelp.sendAnswer(answer)` works only when `sends_answers` is true. `answer` is a string or
  a flat object of name to text, number, boolean or list of text. bighelp shows the exact message and asks
  the person before it sends. It resolves true when sent and false when the person cancels.
- `window.bighelp.close()` closes the app.

The answer reaches the agent as the person's next message. For an object: `My answers to “<title>”:` and then
one `- <name>: <value>` line each (lists joined with ", ", booleans as Yes/No). For a string:
`My answer from “<title>”:` and then the text. The phone writes this message; the plugin never sends one.

## The store

Per agent, in the plugin's state folder for that agent (`plugin-data/loopdy`): `html-apps.sqlite3` (only the
host's user can read it) and one file per page in `html-apps/`, named by app id and SHA-256. A row holds the
id, the chat the app was made in, the title, the description, `sends_answers`, the data source (inline or
`data_path`), the revision, the times it was made and last updated, the page's SHA-256 and byte count, and
inline data with the time it was set. Page files are written whole and moved into place, so a reader never
sees half a page. Each agent keeps at most 200 apps; past that, the least recently updated go (and their pages).

## Routes

`POST /api/plugins/loopdy/native/html-apps/{operation}`. The usual native checks apply: the
`native-html-apps-v1` feature, `If-Match` with the context ETag, and a lowercase UUID in
`X-Loopdy-Request-ID`. Both operations only read. Bodies are camelCase JSON. `agentId` is the profile name.
Responses are `no-store`.

A body that doesn't fit gets `400 invalid_request`. An app that doesn't exist, or belongs to another agent,
gets `404 app_not_found`. An unknown `agentId` gets `404 profile_not_found`.

### `get`

```json
{"agentId": "research", "appId": "a_…", "knownSha256": "<hex, optional>",
 "offset": 0, "htmlSha256": "<hex, for a later piece>"}
```

Answers (at most 4 MiB, whatever the page's size):

```json
{"app": {"appId": "a_…", "title": "Trip planner", "description": "Plan the stops of a road trip.",
         "sendsAnswers": true, "live": false, "revision": 1, "updatedAt": "2026-11-07T12:00:00Z",
         "htmlSha256": "<64 lowercase hex>", "htmlBytes": 2048},
 "offset": 0, "html": "<base64 of up to 2 MiB of the page>", "nextOffset": null}
```

The page comes in pieces of up to 2 MiB, so a page of any allowed size fits the app's 4 MiB cap on one
answer. `nextOffset` is where the next piece starts, or `null` after the last one. The phone asks for each
later piece with its `offset` and the `htmlSha256` of the page it is putting together; when the page changed
meanwhile, that is `409 app_changed`, and the phone starts again. An `offset` past the end is
`400 invalid_request`.

`offset`, `html` and `nextOffset` are left out when `knownSha256` equals `htmlSha256`, so a phone that has
the page asks cheaply.

### `data`

```json
{"agentId": "research", "appId": "a_…"}
```

Answers (at most 2 MiB):

```json
{"appId": "a_…", "data": {"…": "…"}, "updatedAt": "2026-11-07T12:00:00Z",
 "dataSha256": "<hex>", "problem": null}
```

- Inline data: the stored value, the time it was set, and the SHA-256 of its compact JSON.
- `data_path`: the file is read again on every request. `updatedAt` is the file's modified time and
  `dataSha256` is the SHA-256 of its bytes. When the file is gone or outside the working folder (`missing`),
  isn't UTF-8 JSON without NaN or Infinity (`invalid`), or is over 1,048,576 bytes (`too_large`), `data`,
  `updatedAt` and `dataSha256` are null and `problem` says which.
- No data: `data`, `updatedAt`, `dataSha256` and `problem` are all null.

The data keeps its keys in the order the agent wrote them. The app polls this route, and treats a changed
`dataSha256` as new data.

## Privacy

A page can hold what the agent knows about the person. The store is private to the host's user, the routes
answer only the signed-in app, and responses never contain a host path. The plugin logs fixed error codes,
never page or data text.
