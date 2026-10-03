# iPhone tools

Agents can use four tools that run on your iPhone:

| Tool | What it can do |
| --- | --- |
| `iphone_health` | Read Health samples of a given type between two dates (read-only; at most 31 days and 200 records). |
| `iphone_calendar` | List events in a date range, create events, and update or delete an exact event. |
| `iphone_reminders` | List reminders (optionally by list, completion or date), create reminders, and update or delete an exact reminder. |
| `iphone_location` | Where the phone is right now (read-only), for things like "what's good to eat near me?". |

Updates and deletes name the exact item and the revision the agent last saw, so a stale request can't
overwrite a newer change.

## Your control

- Each tool is off until you turn it on for a host in bighelp. Turning one on asks for the iOS permission at
  that moment; the iOS permission alone never gives an agent access.
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

The plugin advertises `native-device-tools-v1` only when the Hermes host supports tool execution middleware.
`native-device-location-v1` comes with it and tells the app that `location` may be in the channel's `enabled`
list; older plugins reject it, so the app leaves Location out for them and asks you to update the plugin.
The routes use the usual native session checks (see [Native workspace API](NATIVE_WORKSPACE_API.md)).
