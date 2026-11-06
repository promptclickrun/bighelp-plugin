# App state

The bighelp app keeps some of its own choices on the computer, so a second
phone, an iPad or a reinstall gets them back: the order of a person's bots,
their sections, which group chats are pinned or hidden, and app settings for
each bot. Before, these lived only on the phone, and an uninstall lost them.

The plugin stores them for each person, not for the computer: two people who
share a computer keep their own layouts. A person is the app's person ID (a
random UUID in the person's iCloud Keychain, the same on all their Apple
devices). The plugin doesn't read the values. It checks only their size and
shape, and that no text in them is shaped like a credential (see Limits).

A person who uses several computers (the app's Multi-gateway) has the same
copy on each one. Every key carries a revision. The app reads each computer,
keeps the newest revision of each key, and writes it to the computers that are
behind. So any one computer is enough to restore everything.

## Where it is kept

At the Hermes root (not a profile's home), so every agent, profile and phone
on the computer sees the same data:

- `plugin-data/loopdy/app-state/computer.json`: this computer's ID, a random
  UUID made the first time the feature is used and never changed. The app
  names bots and chats on any computer by this ID and their own ID, so every
  phone means the same thing however it reaches the computer.
- `plugin-data/loopdy/app-state/people/<person>.json`: one file per person.

The folders are `0700` and the files `0600`. Writes are atomic and take a file
lock, so the plugin's two module copies and every Hermes process agree.

```json
{"schemaVersion": 1,
 "person": "5f0c2a9e-7d1b-4e5a-9c3f-2b8d6e4a1f07",
 "keys": {
   "bots.layout": {"value": {"v": 1, "sections": [{"id": "sec-m3k2a-q8z1x", "name": "Work"}]},
                   "revision": 12, "updatedAt": "2026-11-06T17:02:11Z",
                   "device": "a1d4c3b2-9e8f-4a7b-8c6d-5e4f3a2b1c0d"},
   "bots.settings": {"value": null, "revision": 3, "updatedAt": "2026-11-02T09:15:40Z",
                     "device": "a1d4c3b2-9e8f-4a7b-8c6d-5e4f3a2b1c0d"}}}
```

A key whose value is `null` was deleted. It keeps its revision, so a computer
that still has an older value doesn't bring it back.

## Limits

| What | Limit |
| --- | --- |
| Key name | `^[a-z][a-z0-9]*([.-][a-z0-9]+){0,5}$`, at most 64 characters |
| Keys per person | 24 |
| One value | 65,536 bytes as compact JSON, nesting at most 16 deep |
| One string in a value | 4,096 characters |
| One person's file | 524,288 bytes |
| People per computer | 64 |
| Revision | an integer from 1 to 2^53 − 1 |

Values are any JSON: objects, arrays, strings, finite numbers, booleans and
`null`. Text with control characters is refused, except tab and line breaks.

Text shaped like a credential is refused (`secret_scan_blocked`): a private key
header, `token=` / `password=` / `api_key:` and similar with a value, a link
with a password in it, and known key formats (GitHub, AWS, Slack, `sk-`,
Stripe, JWTs). The free-text rule that also refuses any `name: <long mix of
letters and digits>` is not used here, because the app's values are IDs and
links (`https://…/v2/…`, `<computer>/<profile>`) that it would refuse.

## Routes

`POST /api/plugins/loopdy/native/app-state/<operation>`, feature
`native-app-state-v1`, with the usual native headers (`If-Match` context ETag
and a lowercase `X-Loopdy-Request-ID`). Responses are `no-store` and echo the
request ID. `person` and `device` are lowercase UUIDs.

### `read`

```json
{"person": "<uuid>", "keys": ["bots.layout"]}
```

`keys` is optional; without it every key comes back. A key the person never
wrote is missing from the answer.

```json
{"computerId": "<uuid>",
 "keys": {"bots.layout": {"value": {…}, "revision": 12, "updatedAt": "…", "device": "<uuid>"}}}
```

A person's file that can't be read (not JSON, too big, another schema, a link)
reads as empty, and the log gets a fixed reason code. The next write replaces
it.

### `write`

```json
{"person": "<uuid>", "device": "<uuid>",
 "keys": {"bots.layout": {"value": {…}, "revision": 13, "baseRevision": 12}}}
```

For each key, `baseRevision` is the revision the app built its change on (0
for a key it never saw), and `revision` is the new one. The app picks it: one
more than the newest revision it saw on any computer, so every copy ends up
with the same number. The write is all or nothing. Each key needs:

- the stored revision (0 when there is none) at most `baseRevision`: the app
  has seen what is stored. A computer that is behind takes the write.
- `revision` above the stored revision and above `baseRevision`.

```json
{"computerId": "<uuid>",
 "keys": {"bots.layout": {"value": {…}, "revision": 13, "updatedAt": "…", "device": "<uuid>"}}}
```

If another phone wrote first, nothing is saved and the answer is `409
app_state_conflict`. Its `details.keys` holds the stored entry of each key that
conflicted, so the app can make its change again on top of it and retry.

## Errors

Every error has a fixed code:

- `422 invalid_request`: a bad body, person, device, key name, revision or value
- `422 secret_scan_blocked`: text in a value looks like a key or password
- `413 app_state_too_large`: a value, a string or the person's file is over its limit
- `409 app_state_conflict`: another device wrote a newer revision (`details.keys`)
- `409 store_full`: 24 keys for this person, or 64 people on this computer
- `412 context_changed`, `428 context_required`
- `503 app_state_unavailable`: the feature isn't listed (not a POSIX host)

## What the app keeps here

The app owns these values and their shapes. Each one carries its own `v`:

- `bots.layout`: sections (their IDs, names and order), the section of each
  group chat, the order of the Bots mode home, the primary bot, and which
  group chats are pinned or hidden. Bots' own sections and pins stay on each
  bot's profile (`ui_meta`), where Hermes Desktop reads them.
- `bots.settings`: app settings for each bot, such as its voice choice, its
  Bots mode mark and its pet.

Older apps never call these routes. The app checks `/native/context` for the
feature and, without it, keeps these choices on the phone as before.
