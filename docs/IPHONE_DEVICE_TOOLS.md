# iPhone tools

Agents can use thirteen tools that run on your iPhone:

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
| `iphone_navigation` | Open a page in bighelp to show you where something is, for things like "where do I change the voice?". It only moves between pages. |

bighelp builds from 2.3.0 (105) did not offer Health, and app builds that bring it back
([promptclickrun/bighelp#750](https://github.com/promptclickrun/bighelp/issues/750)) turn it on again. The plugin's
side of `iphone_health` and `health.read` did not change.

Updates and deletes name the exact item and the revision the agent last saw, so a stale request can't
overwrite a newer change.

## Your control

- Each data tool is off until you turn it on for a host in bighelp. Turning one on asks for the iOS permission at
  that moment; the iOS permission alone never gives an agent access.
- When an agent calls a data tool that is off, a bighelp build that can ask shows a card in the chat with
  **Turn on** and **Not now** (see Asking to turn a tool on). Turn on sets the same Device access switch and
  then runs the call. The card only asks; it is not a second setting.
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
  switching hosts or turning a tool off stops them, and anything in flight is dropped rather than retried later.
  When the app goes to the background, or the phone is away for a short time, a call waits a little for the
  phone to come back (see Waiting), but only if the phone has not received it yet.
- If the phone isn't available, the agent gets a clear "phone unavailable" result, never made-up or empty data.
  The result tells the agent what to say: `payload.note` is "bighelp isn't open on the phone right now. Ask the
  person to open bighelp, then try again." Apple doesn't reveal whether Health read access was denied, so an empty
  Health result can mean either no data or no access.

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

## Navigation

`iphone_navigation` shows a person the way in the app. It has two operations:

- `operation: "places"` finds pages by the person's words. It takes an optional `query` (1–120) and an optional
  `limit` (1–20, default 8). The phone answers with the best matches and how many there are in all:

  ```json
  {
    "places": [
      {
        "id": "settings.appearance",
        "title": "Appearance",
        "what": "How bighelp looks.",
        "steps": ["Open the menu", "Tap Settings", "Tap Appearance"],
        "needs": null
      }
    ],
    "total": 1
  }
  ```

  `needs` says what a page needs before it opens (for example Nerd Mode), or is `null`.

- `operation: "open"` takes `place` (required: an ID from `places`, 1–96 characters, only lowercase letters, digits,
  `.` and `-`), `bot` (an agent ID, 1–96, for a page inside one bot such as its profile) and `note` (1–160: one
  short sentence the app shows beside the page). The phone answers `{"opened": "<place>", "steps": [...], "mode":
  "bots" | "chat" | "code"}`.

- It only moves between pages and points at controls. It never changes a setting or taps Save, Send or Delete.
  The app does not journal either operation: doing one twice is the same as doing it once.
- The plugin refuses unknown keys, a wrong type or a value out of range (`invalid_arguments`) before the call
  leaves the host. The phone owns the list of pages, so it answers `unknown_place` for an ID it does not have.
  Its other failures are `not_in_this_mode`, `needs_nerd_mode`, `needs_plugin_update`, `needs_bot` and `busy`.
- Also the usual codes: `authorization_required` when the app is too old to offer it, `phone_unavailable` or
  `device_unavailable` when bighelp isn't open.

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
| `navigation.places` | `query` (1–120), `limit` (1–20, default 8) |
| `navigation.open` | `place` (1–96; lowercase letters, digits, `.` and `-`), `bot` (1–96), `note` (1–160) |
| `reminders.create` and `reminders.update` | also `locationTrigger` `{name, latitude, longitude, radiusMeters (100–5000), proximity: "arrive" or "leave"}`; `reminders.update` also takes `clearLocationTrigger: true` (not together with `locationTrigger`) |

Writes (create, update, delete, save, play and the like) are never repeated by the plugin. When the outcome is
uncertain, the agent reads the current state before it tries again.

In a chat that holds a picture the person sent (or a photo the agent read with `photos.get`),
`contacts.create` and `calendar.create` fail with `failed` and code `review_required`: something read from a
picture is saved only after the person reviews it. The host answers first, without sending the call to the
phone, when the chat's history shows such a picture; the phone refuses it the same way. Either way the
result's `payload.note` tells the agent to make a draft with `bighelp_draft_contact` or `bighelp_draft_event`
instead and not to try the tool again. Every other operation, and a create in a chat without a picture,
reaches the phone as before; when the history can't be read, the phone decides. See
[Photo actions](IMAGE_DRAFTS.md).

### Photos

- `photos.get` returns the item's fields and, from the phone, a small JPEG (`image.data`, base64, at most 90 KB
  with sides of at most 2,048 pixels). The plugin never passes those bytes on. It checks they are a whole JPEG,
  writes the picture to `bighelp/phone-photos/` in the serving agent's Hermes home (named by its content hash,
  owner-only), and gives the agent `image: {format, width, height, path}` so it can look at the picture with its
  vision tool. The folder keeps the newest 50 pictures from the last 7 days. A picture that isn't a JPEG within the
  limits is `photo_unreadable`. `photos.get` never gives the agent picture data, even in a failed result.
  The plugin also notes which chat read the picture (`bighelp/phone-photos-fetched.sqlite3`), so that chat's
  agent can make a contact or event draft from it ([Photo actions](IMAGE_DRAFTS.md)).
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

If a call finds no open channel for its chat but the chat's phone had one in the last 120 seconds (the lease
lapsed, the app went to the background, or the same phone is opening the chat again), the call waits for that
same phone to open a channel for that chat again. The same phone means the same signed-in person, `deviceId` and
`hostId`. The wait ends at the call's own deadline, 30 seconds from the call (70 for `shortcuts.run` and
`photos.save`), and never later. A different phone or person that opens the chat does not get the call, and the
call fails with `phone_unavailable`. With no recent channel, the call fails at once. Advertised as
`native-device-tools-wait-v1`.

### Asking to turn a tool on

A phone can tell the plugin which tools are off but can be turned on with a card in the chat. The plugin
advertises `native-device-permission-request-v1` for this, under the same condition as `native-device-tools-v1`,
and leaves it out (with the same reason) when device tools are unavailable.

- `connect` takes an optional `askable` list. It holds the kinds the phone can ask the person to turn on: only
  the kinds that share data, `health`, `calendar`, `reminders`, `location`, `contacts`, `photos`, `geofences`,
  `alarms`, `music`, `focus` and `shortcuts` (never `appearance` or `navigation`). The list is unique, has at most
  11 kinds and must not share a kind with `enabled`; otherwise `connect` fails with `invalid_request`. It is empty
  when left out, and `enabled` may be empty while `askable` is not. The body of `connect` refuses fields it does
  not know, so an app sends `askable` only when the plugin lists the feature.
- A call for a kind in `enabled` works as before. A call for a kind in `askable` is queued as usual, but its
  request has a window of 300 seconds (`expiresAt` is `sentAt` + 300; Hermes' `manage_connections` also waits
  5 minutes). The channel's lease lasts until that deadline, because the phone stops polling while its card is
  open. The host waits for the result up to that deadline. A call for any other kind fails with
  `authorization_required`, as before.
- The phone shows the card. After Turn on it runs the call and returns the call's own result on the same
  channel. A `result` is accepted for a kind in `enabled` or `askable`. A new `connect` (to send the new `enabled`
  list, say) ends the old channel, and a request the phone already received then fails, as in the table below,
  so the phone answers first and connects again after.
- Otherwise the phone answers `failed` with one of these codes. The plugin puts a `payload.note` on each, so
  the agent knows what to say:

  | Code | When | `payload.note` |
  | --- | --- | --- |
  | `permission_declined` | The person tapped Not now, now or earlier in this chat. | The person chose not to turn on this iPhone tool in this chat. Answer without it, and don't call it again in this chat unless they ask. |
  | `permission_denied` | iOS said no. | iOS didn't allow bighelp to use this. The person can allow it in the iPhone's Settings app under bighelp, then ask again. |
  | `permission_timeout` | Nobody answered the card. | Nobody answered the request to turn on this iPhone tool. Ask the person if they want to try again. |

- If the phone never answers by the deadline, the host gives up as for any other call: `phone_unavailable` for a
  pure read, `outcome_unknown` for anything else the phone received.
- A call that waits for its phone (see above) remembers the chat's `askable` list too. It gets the 300-second
  window, and the new channel takes it if the kind is in its `enabled` or `askable` list. The channel's lease
  then lasts until the call's deadline when the kind is still askable.
- Every other request keeps `20 <= expiresAt - sentAt <= 60`; only these permission requests may have up to 300.
- An older phone sends no `askable` list and gets `authorization_required` for a tool that is off. An older
  plugin does not list the feature, so the phone does not send `askable` to it.

The card stores none of the person's data. It only asks, and Turn on changes the one Device access switch.

A channel can end in four ways:

| How it ends | A request the phone has not received | A request the phone already received |
| --- | --- | --- |
| The lease lapses (no poll for 30 seconds) | Waits for the same phone's next channel for the chat | Fails |
| The same phone opens the chat again | Moves to the new channel at once | Fails |
| `close` with `reason: "paused"` (the app went to the background and will be back) | Waits for the same phone's next channel for the chat | Fails |
| `close` without a reason, an owner change, or the plugin unloading | Fails | Fails |

A request the phone has received is one that a `poll` answer has handed to it. It may have run, so it is never
sent again. Only a pure read fails with `phone_unavailable`, and the agent may ask again. The pure reads are
`health.read`, `calendar.list`, `reminders.list`, `location.current`, `appearance.get`, `contacts.list`,
`photos.list`, `photos.albums`, `photos.get`, `geofences.list`, `alarms.list`, `music.nowPlaying`, `music.search`,
`music.library`, `focus.current`, `shortcuts.list` and `navigation.places`. Every other operation (and any operation the plugin does not
know) fails with `outcome_unknown`, whose note tells the agent not to run it again and to check first. A request
the phone never received did nothing, so it fails with `phone_unavailable` (except `shortcuts.run` and
`photos.save`, which always say `outcome_unknown` when they time out). A request whose time ran out is also no
longer handed out by `poll`.
A request that waits keeps its `sentAt` and `expiresAt`. It gets a new `requestId`, because the ID names the
channel, so an answer for the old channel is not taken on the new one. A retry of the same Hermes tool call (the
same session, turn and tool-call ID) joins the waiting request, and a call that already reached the phone is not
run again by a retry (`result_replay`).

### Tool schemas

The schemas the agent reads are short on purpose, because they are sent with every request to the serving agent
(about 11 KB for all `iphone_*` tools). They keep the enums, number ranges and list sizes, and leave out formats,
patterns, long text limits and nested shapes (a contact's `phones` is described as `[{label, value}]`). The plugin
checks every bound in the table above, and `invalid_arguments` names the field that was wrong in `payload.note`.

## How it works

The phone opens a short-lived channel for the exact agent and chat it has open, then keeps it alive:

- `POST /api/plugins/loopdy/native/device-tools/connect` with the chat's agent and session, the tools you
  enabled and, on plugins that list `native-device-permission-request-v1`, the tools it may ask for (`askable`)
- `…/poll` to pick up requests, and `…/result` to return each answer
- `…/close` when the chat closes. The optional `reason: "paused"` says the app went to the background and will
  be back; no other reason is accepted (`invalid_request`, like any other unknown field). The body of `close`
  refuses fields it does not know, so an app sends `reason` only when `native-device-tools-wait-v1` is listed.

A channel expires after 30 seconds without a poll. When an agent calls an `iphone_*` tool, Hermes' public tool
execution middleware hands the call to the phone that holds a channel for that exact agent and session, using
Hermes' own session, turn and tool-call IDs. Nothing is taken from the model's arguments to decide which phone
to use. With no open channel and no recent one (see Waiting), the tool fails with `phone_unavailable`. A call the phone doesn't answer in time fails
rather than being retried.

One chat has one phone. A `connect` for a chat that another phone holds fails with `channel_ambiguous`: the plugin
never picks a phone by which one came last. The same phone opening the chat again (the same signed-in person,
`deviceId` and `hostId`) replaces its own earlier channel, for example after the app closed before its `close`
arrived. A call the old channel's phone has not received yet moves to the new channel. A call it has received fails
with `phone_unavailable`, as it does when that channel closes, and the old channel's `close` answers
`phone_unavailable` too.

The plugin advertises `native-device-tools-v1` only when the Hermes host supports tool execution middleware.
`native-device-location-v1` comes with it and tells the app that `location` may be in the channel's `enabled`
list; older plugins reject it, so the app leaves Location out for them and asks you to update the plugin.
`native-device-appearance-v1` does the same for `appearance`, and so do `native-device-contacts-v1`,
`native-device-photos-v1`, `native-device-geofences-v1`, `native-device-alarms-v1`, `native-device-music-v1`,
`native-device-focus-v1`, `native-device-shortcuts-v1` and `native-device-navigation-v1` for `contacts`, `photos`,
`geofences`, `alarms`, `music`, `focus`, `shortcuts` and `navigation`. A call whose kind isn't in the channel's `enabled` list fails with `authorization_required`.
Because Appearance is on by default, the phone opens a
channel for every chat on a host that lists it, and polls more slowly (every 8 seconds instead
of 0.75) while Appearance is the only tool on that channel.

### Every agent

Hermes loads the plugin once for each agent (profile) that has it, and each copy has its own tools. The phone's
routes reach only the copy of the agent that serves the app (`servingProfileId` in `/native/context`, usually
`default`). So the plugin keeps one hub for the whole Hermes process, and every copy of the same hub version uses
it. Each agent's tools name their own profile (Hermes' `profile_name`, never a value from the model), and a call
reaches only a channel for that profile and chat.

A phone may open a channel (`connect`) with an `agentId` that is any agent of this host, if that agent's own session
store (read-only) has the `sessionId`. Everything else is as before: the same person must own the channel, a second
phone for one chat fails with `channel_ambiguous`, and the same phone replaces its own channel. These answers are
new or changed:

| Code | Status | When |
| --- | --- | --- |
| `profile_not_found` | 404 | `agentId` is not an agent of this host. The plugin never uses another agent instead. |
| `session_not_found` | 404 | The `sessionId` is not in that agent's session store. |

`profile_not_served` (403) is no longer sent. The plugin advertises `native-device-tools-agents-v1` when it works
this way. An agent that has no bighelp plugin has no `iphone_*` tools, so the phone has nothing to call there. A
picture from `photos.get` goes to `bighelp/phone-photos/` in the Hermes home of the agent that asked, and the
checks of `photos.save` run in that agent's home too.

The hub is shared only by copies of the plugin with the same hub version and the same hub code. An agent whose
Hermes home carries an older copy of the plugin (one from before this feature, or one that was not updated) keeps a
private hub. `/native/context` still lists `native-device-tools-agents-v1`, because it describes the serving
copy, but the phone tools of that agent answer `phone_unavailable`. Update the plugin in that agent's home (a link
to the default agent's copy, see [Shared plugins](SHARED_PLUGINS.md), updates it too).

A `connect` for a profile with no `state.db` yet (no chat has been saved) is accepted when the chat is live in
Hermes. A chat that is in neither place is `session_not_found`.
The routes use the usual native session checks (see [Native workspace API](NATIVE_WORKSPACE_API.md)).
