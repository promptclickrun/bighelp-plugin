# My Templates

People can save a bot as a template from the bighelp app (a bot's profile,
Share) and start new bots from it later. Saved templates live on this
computer, not on the phone, so every phone and every agent on the computer
sees the same list. The app shows them as "My Templates" at the top of its
template picker, next to the public Template Catalog.

## What a template holds

The Template Catalog's agent shape, so the app opens a saved template the same
way as a catalog one:

| Field | Limit | Notes |
| --- | --- | --- |
| `name` | 40 | Required. Plain text, one line, no `{{…}}` |
| `role` | 160 | What the bot is for |
| `vibe` | 160 | How it sounds |
| `description` | 400 | Shown on its card |
| `instructions` | 20,000 | Required. The bot's personality (SOUL), with `{{agent_name}}` where the name goes |
| `symbol` | 60 | Optional SF Symbol name |
| `category` | 40 | Optional |
| `variables` | 12 | Optional fill-in fields, as in the catalog's [template variables](https://github.com/promptclickrun/bighelp/blob/main/services/catalog/docs/TEMPLATE_VARIABLES.md) |

Text is reduced to plain text (control and direction characters go). A
template that looks like it holds a key, token or password is refused. Pictures,
models, skills, memories and settings are not saved: a bot made from a template
still starts from the computer's usual setup.

## Where it is kept

One JSON file per template at the Hermes root (not a profile's home):
`plugin-data/loopdy/my-templates/<id>.json`, where `<id>` is a lowercase UUID.
The folder is `0700` and each file `0600`. Writes are atomic and take a file
lock, so the plugin's two module copies and every Hermes process agree. At
most 40 templates, 128 KB each.

```json
{"schemaVersion": 1,
 "template": {"id": "…", "name": "Trip Planner", "role": "Travel helper",
              "vibe": "Organized, upbeat", "description": "Saved from Juniper",
              "instructions": "# {{agent_name}}\n\nYou are {{agent_name}}…",
              "source": "mine", "updatedAt": "2026-10-14T17:46:40Z"}}
```

## A broken file never breaks the list

Reading is per file and lenient. A file that is not valid JSON, not UTF-8,
too big, a link, a folder, named oddly, from another schema version, missing
its name or instructions, holding a key, or with an ID that doesn't match its
file name is skipped and counted in `skipped`. The rest still list. The log
gets a fixed reason code, never the file's name or contents. A broken file can
still be deleted by its ID.

## Routes

`POST /api/plugins/loopdy/native/my-templates/<operation>`, feature
`native-my-templates-v1`, with the usual native headers (`If-Match` context
ETag and a lowercase `X-Loopdy-Request-ID`). Responses are `no-store` and
echo the request ID.

| Operation | Request | Response |
| --- | --- | --- |
| `list` | `{}` | `{"templates": [template, …], "skipped": 0}`, newest first, at most 4 MB |
| `save` | `{"template": {name, role, vibe, description, instructions, …}}` | `{"template": template}` with its new `id`, `source: "mine"` and `updatedAt` |
| `delete` | `{"id": "<uuid>"}` | `{"id": "<uuid>", "deleted": true}` (`false` when it was already gone) |

Errors carry a fixed code:

- `422 invalid_request`: a bad body or ID
- `422 invalid_template`: a missing name or instructions, an unknown field or text over its limit
- `422 secret_scan_blocked`: the template looks like it holds a key or password
- `413 template_too_large`: instructions or the whole template are too long
- `409 store_full`: 40 templates already; delete one first
- `412 context_changed`, `428 context_required`
- `503 my_templates_unavailable`: the feature isn't listed (not a POSIX host)

Older apps never call these routes; the app checks `/native/context` for the
feature and asks people to update the plugin when it's missing.
