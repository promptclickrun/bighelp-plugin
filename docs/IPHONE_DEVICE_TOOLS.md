# iPhone tools

Agents can use twelve tools that run on your iPhone:

| Tool | What it can do |
| --- | --- |
| `iphone_health` | Read Health samples of a given type between two dates (read-only; at most 31 days and 200 records). |
| `iphone_calendar` | List events in a date range, create events, and update or delete an exact event. |
| `iphone_reminders` | List reminders (optionally by list, completion or date), create reminders, and update or delete an exact reminder. |
| `iphone_location` | Where the phone is right now (read-only), for things like "what's good to eat near me?". |
| `iphone_appearance` | See or change how bighelp looks: light or dark mode and the chat color, for things like "make it violet, dark mode". |
| `iphone_contacts` | Find, add, change and delete contacts. |
| `iphone_photos` | Look through the Photos library, look at a small copy of a picture, save a picture or video from the host, tidy albums and favorites, and delete (iOS asks you first). |
| `iphone_geofences` | Watch places and send a message to the chat when you arrive or leave. |
| `iphone_alarms` | Set, change and remove bighelp's own alarms and timers (iOS 26 and later). |
| `iphone_music` | See what plays, search, play and control music, and add to playlists and the library. |
| `iphone_focus` | Read which Focus is on and your note for agents, so agents can respect it (read-only). |
| `iphone_shortcuts` | List and run only the Shortcuts you chose. |

Current bighelp builds do not offer Health. Only app builds before 2.3.0 (105) can turn on `iphone_health`.

Updates and deletes name the exact item and the revision the agent last saw, so a stale request can't
overwrite a newer change.

## Your control

- Each data tool is off until you turn it on for a host in bighelp. Turning one on asks for the iOS permission at
  that moment; the iOS permission alone never gives an agent access.
- Appearance shares none of your data and every change is easy to undo, so it's on until you turn off "Let agents
  change how bighelp looks" in Settings › Device access. That switch counts for every host. You can change the look
  back at any time in Appearance.
- Contacts, Photos, Arriving and leaving, Alarms and timers, Music, Focus and Shortcuts each have their own switch,
  off until you turn it on. Focus is read-only: no app can turn a Focus on or off. Alarms see only the alarms
  bighelp made, never the Clock app's. Shortcuts run only the names you typed in Settings › Device access.
- Health and Location are read-only. Calendar and Reminders can make changes once enabled, without asking each time.
- Location uses iOS's "while using the app" permission only, never "always", and nothing tracks you in the
  background. bighelp shares approximate location by default; on each call iOS may ask whether to share your
  precise location this time. If you keep it approximate, the agent gets only a rough area.
- The tools only work while bighelp is open, unlocked and showing that host's chat. Leaving the chat,
  switching hosts, turning a tool off or backgrounding the app stops them, and anything in flight is dropped
  rather than retried later.
- If the phone isn't available, the agent gets a clear "phone unavailable" result, never made-up or empty data.
  Apple doesn't reveal whether Health read access was denied, so an empty Health result can mean either no
  data or no access.

What the tools return goes to your Hermes host and its AI provider, and may stay in the chat's normal history.
The app keeps only small records needed to avoid repeating a change, never the Health, calendar, reminder or
location contents.

## Location

`iphone_location` takes one argument, `operation: "current"`. A completed result's `payload` looks like this
(made-up values):

```json
{
  "latitude": 12.3456,
  "longitude": -65.4321,
  "horizontalAccuracyMeters": 25,
  "timestamp": "2026-10-03T17:00:00Z",
  "precise": true,
  "place": {
    "street": "100 Example Street",
    "neighborhood": "Harbor District",
    "city": "Sampleton",
    "region": "Example State",
    "country": "Exampleland",
    "postalCode": "00000"
  }
}
```

- `precise` is false when the person kept approximate location. Then `horizontalAccuracyMeters` is large (often a
  few kilometers), `note` says it's a rough area, and `place` has no street or neighborhood.
- `place` comes from Apple's reverse geocoding. It's left out when Apple can't name the spot, and any of its fields
  can be missing.
- Failures use the usual codes: `authorization_required` when Location is off or iOS access was denied,
  `phone_unavailable` or `device_unavailable` when bighelp isn't open, and `unavailable` when iOS can't get a fix.

## Appearance

`iphone_appearance` has two operations:

- `operation: "get"` (no other arguments) returns how bighelp looks now and what an agent can choose:

  ```json
  {
    "mode": "system",
    "color": "lavender",
    "modes": ["system", "light", "dark"],
    "colors": ["lavender", "grape", "ocean", "sky", "teal", "mint", "lime", "sunflower", "tangerine", "coral", "rose", "ink"]
  }
  ```

- `operation: "set"` takes `mode` (`system`, `light` or `dark`), `color`, or both. `color` is one of the app's color
  names (any case) or `#rrggbb` for any other color. The phone applies them at once and returns the look it shows
  now, for example `{"mode": "dark", "color": "rose"}` or `{"mode": "dark", "color": "#0E7C66"}`.

- The color is the person's message bubbles and buttons, like a color picked in Appearance. The page color
  doesn't change.
- The plugin refuses a `set` with nothing to change, an unknown mode or a malformed color (`invalid_arguments`)
  before it reaches the phone. The app owns its color names, so the phone answers `unknown_color` for a name it
  doesn't have and changes nothing; `get` lists the names.
- Failures use the usual codes: `authorization_required` when the person turned it off (or their app is too old
  to offer it), `phone_unavailable` or `device_unavailable` when bighelp isn't open, and `owner_changed` when the
  person left the chat before the change.

## Arguments the plugin checks

Every `iphone_*` tool sends `operation` plus the arguments below. The phone's operation is `<kind>.<operation>`
(`iphone_photos` `createAlbum` is `photos.createAlbum`). The plugin checks the arguments against these limits
before a call leaves the host (`invalid_arguments`), and the phone checks them again. Arguments are at most 16 KB.
Dates are ISO-8601 with an offset or `Z`. IDs are opaque strings of at most 512 characters; use the exact ID and
`expectedRevision` that a list returned.

| Operation | Arguments |
| --- | --- |
| `contacts.list` | `query` (1–200), `limit` (1–50, default 20) |
| `contacts.create` | at least one of `givenName`, `familyName`, `organization` (each ≤ 200); optional `nickname`, `jobTitle`, `birthday` (`YYYY-MM-DD` or `--MM-DD`), `phones` (≤ 10), `emails` (≤ 10), `urls` (≤ 5), `addresses` (≤ 5). A list item is `{label, value}`; an address is `{label, street, city, region, postalCode, country}`. |
| `contacts.update` | `id`, `expectedRevision`, and at least one `create` field. A list you give replaces the whole list; an empty string or list clears it. |
| `contacts.delete` | `id`, `expectedRevision` |
| `photos.list` | `start` and `end` together, `albumID`, `mediaType` (`image`, `video`, `any`), `favorites`, `limit` (1–100) |
| `photos.albums` | none |
| `photos.get` | `id`, `maxPixels` (256–1024) |
| `photos.save` | `path` (a file on this host), `albumID` |
| `photos.update` | `id`, `expectedRevision`, and at least one of `favorite`, `addToAlbumIDs`, `removeFromAlbumIDs` (≤ 20 each) |
| `photos.createAlbum` | `title` (1–200) |
| `photos.delete` | `ids` (1–50). iOS asks the person to confirm; Don't Allow is `cancelled`. |
| `geofences.list` | none |
| `geofences.create` | `name` (1–100) and either `latitude` and `longitude` or `here: true`; `radiusMeters` (100–5000), `on` (`arrive`, `leave`, `both`), `note` (≤ 1000), `repeats` |
| `geofences.delete` | `id` |
| `alarms.list` | none |
| `alarms.create` | `label` (1–60) and exactly one of `time` `{hour, minute}` (with optional `weekdays`, `monday` … `sunday`), `date`, `timerSeconds` (5–86,400) |
| `alarms.update` | `id` and at least one `create` field; a new schedule replaces the old one |
| `alarms.delete` | `id` |
| `music.nowPlaying` | none |
| `music.search` | `query` (1–200), `types` (`songs`, `albums`, `playlists`, `artists`), `source` (`catalog`, `library`), `limit` (1–25) |
| `music.library` | `type` (`playlists`, `recent`), `limit` (1–50) |
| `music.play` | `query`, or `id`, `type` and `source` exactly as a search result gave them; `shuffle` |
| `music.control` | `action` (`pause`, `resume`, `next`, `previous`, `stop`) |
| `music.createPlaylist` | `name` (1–100), `description` (≤ 300), `itemIDs` (≤ 100) |
| `music.addToPlaylist` | `playlistID`, `itemIDs` (1–100) |
| `music.addToLibrary` | `id`, `type` |
| `focus.current` | none |
| `shortcuts.list` | none |
| `shortcuts.run` | `name` (one the list returned), `input` (≤ 4000) |
| `reminders.create` and `reminders.update` | also `locationTrigger` `{name, latitude, longitude, radiusMeters (100–5000), proximity: "arrive" or "leave"}`; `reminders.update` also takes `clearLocationTrigger: true` (not together with `locationTrigger`) |

Writes (create, update, delete, save, play and the like) are never repeated by the plugin. When the outcome is
uncertain, the agent reads the current state before it tries again.

### Photos

- `photos.get` returns the item's fields and, from the phone, a small JPEG (`image.data`, base64, at most 90 KB
  with sides of at most 2,048 pixels). The plugin never passes those bytes on. It checks they are a whole JPEG,
  writes the picture to `bighelp/phone-photos/` in the serving agent's Hermes home (named by its content hash,
  owner-only), and gives the agent `image: {format, width, height, path}` so it can look at the picture with its
  vision tool. The folder keeps the newest 50 pictures from the last 7 days. A picture that isn't a JPEG within the
  limits is `photo_unreadable`. `photos.get` never gives the agent picture data, even in a failed result.
- `photos.save` takes a file on the host. The plugin checks it the way `attachments/resolve` checks files an agent
  sends: the gateway's delivery policy decides which files an agent may send, the file must be a picture or video,
  and it can be at most 50 MB. Refusals are `invalid_arguments`, `unsupported_media` (including kinds the gateway doesn't deliver, such as
  `.heic`), `file_too_large` and `files_unavailable` (the attachment routes can't run), and the phone is not
  asked. The phone gets the resolved absolute path and downloads it through `attachments/resolve` and
  `attachments/fetch` for the chat; that chat may resolve the path for 10 minutes, even though no message of the
  agent names it.

### Waiting

A call waits up to 30 seconds for the phone. `shortcuts.run` and `photos.save` give the phone 60 seconds (the
request's `expiresAt` stays within 60) and the host waits 10 seconds longer, so a slightly late answer still
counts. While one of those waits, the channel's lease lasts for the whole wait: the phone may be in another app.
If the phone never answers one of them, the agent gets `outcome_unknown` (not `phone_unavailable`): the shortcut
may have run or the file may have been saved. The result says not to run it again and to check first.

### Tool schemas

The schemas the agent reads are short on purpose, because they are sent with every request to the serving agent
(about 10 KB for all `iphone_*` tools). They keep the enums, number ranges and list sizes, and leave out formats,
patterns, long text limits and nested shapes (a contact's `phones` is described as `[{label, value}]`). The plugin
checks every bound in the table above, and `invalid_arguments` names the field that was wrong in `payload.note`.

## How it works

The phone opens a short-lived channel for the exact agent and chat it has open, then keeps it alive:

- `POST /api/plugins/loopdy/native/device-tools/connect` with the chat's agent and session and the tools you
  enabled
- `…/poll` to pick up requests, and `…/result` to return each answer
- `…/close` when the chat closes

A channel expires after 30 seconds without a poll. When an agent calls an `iphone_*` tool, Hermes' public tool
execution middleware hands the call to the phone that holds a channel for that exact agent and session, using
Hermes' own session, turn and tool-call IDs. Nothing is taken from the model's arguments to decide which phone
to use. With no open channel, the tool fails with `phone_unavailable`. A call the phone doesn't answer in time fails
rather than being retried.

One chat has one phone. A `connect` for a chat that another phone holds fails with `channel_ambiguous`: the plugin
never picks a phone by which one came last. The same phone opening the chat again (the same signed-in person,
`deviceId` and `hostId`) replaces its own earlier channel, for example after the app closed before its `close`
arrived. A call that waits on the old channel fails with `phone_unavailable`, as it does when that channel closes,
and the old channel's `close` answers `phone_unavailable` too.

The plugin advertises `native-device-tools-v1` only when the Hermes host supports tool execution middleware.
`native-device-location-v1` comes with it and tells the app that `location` may be in the channel's `enabled`
list; older plugins reject it, so the app leaves Location out for them and asks you to update the plugin.
`native-device-appearance-v1` does the same for `appearance`, and so do `native-device-contacts-v1`,
`native-device-photos-v1`, `native-device-geofences-v1`, `native-device-alarms-v1`, `native-device-music-v1`,
`native-device-focus-v1` and `native-device-shortcuts-v1` for `contacts`, `photos`, `geofences`, `alarms`, `music`,
`focus` and `shortcuts`. A call whose kind isn't in the channel's `enabled` list fails with `authorization_required`.
Because Appearance is on by default, the phone opens a
channel for every chat with the serving agent on a host that lists it, and polls more slowly (every 8 seconds instead
of 0.75) while Appearance is the only tool on that channel.

Hermes loads the plugin, and so the `iphone_*` tools, only into the agent it serves (`servingProfileId` in
`/native/context`, usually `default`). The plugin refuses a channel for any other agent (`profile_not_served`), and
the phone doesn't ask: in another agent's chat it opens no channel, and Settings › Device access names the agent
that can use the shared data.
The routes use the usual native session checks (see [Native workspace API](NATIVE_WORKSPACE_API.md)).
