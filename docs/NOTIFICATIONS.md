# Notifications and Live Activities

Optional. Chat works without them. When you turn notifications on for a host in bighelp, the host can send
your phone alerts about that agent's chats, and update a Live Activity while it works.

## Who does what

- **BuzzKit** delivers the push to your phone.
- **bighelp's notification service** (`https://link.loopdy.app`; the name is historical) holds the BuzzKit
  sending key, checks that the host is allowed to notify you, and forwards each alert. The host never holds
  BuzzKit or Apple push keys.
- **Your host** decides what's worth an alert and, with current app builds, encrypts it so only your phone can
  read it.
- **The app** shows the alert. Tapping one re-checks the event with your host before opening the chat.

## Setting it up

1. The app reads the host's `GET /api/plugins/loopdy/notifications/capabilities`: the host's notification key,
   the event types it supports, and whether it can seal alerts (`"sealedAlerts": {"version": 2}`).
2. The app asks the notification service for a **grant**: permission for this host's key to notify this phone,
   for one agent profile, chosen event types, and at most 30 days.
3. The app gives the grant to the host (`POST /notifications/enroll`). The host confirms it with the service
   by signing with its own key.
4. If the host can seal alerts, the app sends its own content key straight to the host
   (`PUT /notifications/enrollments/<grantId>/recipient-key`). It never goes through the service.
5. When you open a chat, the app subscribes that chat (`PUT /notifications/enrollments/<grantId>/sessions`).
   Only subscribed chats produce alerts.

Turning notifications off revokes the grant with the service, so the host can no longer send alerts for it.
Removing the grant on the host (`DELETE /notifications/enrollments/<grantId>`) also deletes the host's local
copy, including the phone's content key.

## Events

| Event | When |
| --- | --- |
| `session.completed` / `session.failed` | The agent finished a reply, or couldn't. |
| `scheduled.completed` / `scheduled.failed` | A scheduled task finished or failed. |
| `approval.required` | The agent needs your approval to continue. |
| `clarification.required` | The agent asked you a question. |
| `subagent.completed` / `subagent.failed` | A helper the agent started finished or failed. |

Each alert shows the agent's name, picture and the start of its message (up to 1,600 characters). Event IDs
are stable (`<grantId>:<sha256>`), so a retry never produces a second alert.

### Cards in a reply

A card reaches the chat as a `loopdy-card` fence of JSON inside the agent's reply. The alert never shows that
JSON: each card reads as a short plain preview where it sits, and the reply's own words stay as they are
([`card_previews.py`](../loopdy_plugin/card_previews.py)). A reply with several cards is still one alert.

- **Authored:** a renderer call can carry an optional `notification_text` (plain text, at most 300 characters).
  It stays out of the card, so the card and its hash are unchanged and older apps see nothing new. The preview
  is found by the card's exact identity: the reply's card must match that renderer call's result, by canonical
  JSON, in the same chat's Hermes history. A card rendered but not sent lends nothing, and nothing is kept
  outside Hermes' history.
- **Made from the card:** otherwise the preview comes from the card's own fields: a generic card's title and
  `spoken_summary`, a forecast's temperature and conditions, a score, a price, a checklist's progress, or a
  card's title and description. Identifiers, form requests, job prompts and other internal fields are never
  read.
- **Neutral:** a card the app wouldn't draw (unknown, edited or broken JSON, a fence that never closes) reads
  "Sent a card.".

Card previews change only the alert's text. Which turns alert, who gets them, their timing and their count are
unchanged, rendering a card never sends anything by itself, and tapping the alert opens the same chat. Replies
without a card alert exactly as before. A preview is plain text, at most 300 characters per card and eight
cards per alert, inside the alert's usual 1,600-character and 2,300-byte limits.

A reply, task or helper alert goes out from the Hermes process that ran the turn as soon as the reply is saved.
One Hermes process also owns a background sender that retries anything that didn't go out; both claim each alert
before sending, so it's sent once. Approval alerts wait a short grace period and stay with that sender.

### Silent replies

Agents can answer with a silence marker such as `[SILENT]` or `NO_REPLY` when there's nothing to say. A reply
alert follows Hermes' own delivery rules (`gateway/response_filters.py` on the host), so the phone never shows a
marker Hermes itself wouldn't send:

- **Scheduled tasks and webhooks** use Hermes' loose rule. A reply that is a marker, starts with `[SILENT]`, or has
  a marker on its own first or last line sends no alert.
- **Other chats** use Hermes' exact rule: only a reply that is just a marker counts. It stays silent when no
  person is waiting for an answer, for example an off-screen note like a widget tap, a Hermes internal notification,
  or a group message that wasn't addressed to the agent. When a person's message got only a marker, the alert
  carries Hermes' notice instead ("The model returned only a silence marker…"). Hermes versions before that notice
  keep every bare marker silent.
- **Group chats:** Hermes tells an agent with nothing new to add to reply `(pass)`. A group chat reply that is
  just a pass (`(pass)`, `pass` or `Pass.`, any case) sends no alert, the same as the chat, which shows nothing for
  it. Anywhere else, "Pass." is an answer and alerts.
- Failed runs always alert.

### Staying signed in

A rotating sign-in, like the Nous Portal's whose renewal token lasts a day, runs out if bighelp stays closed.
So every eight hours the background sender asks the service to wake each enrolled phone (one with a content
key) with a quiet push: `POST /v1/notifications/host-grants/<grantId>/wake` with
`{"version": 1, "reason": "renew-sign-in"}`. The push has no text, sound or topic, only
`bighelp_wake: {version: 1, type: "renew-sign-in", grantId}`, and the service sends at most one per grant every
six hours. The app then renews its sign-in to each computer it isn't connected to. A refused wake (an older
service answers 404) waits for the next interval and never removes the phone's enrollment.

### Peer chats

When agents message each other (`hermes peer dm`, or one agent DMing another), the turns land in Hermes'
canonical "Bot Chat" session. Replies, failures and helper results there alert only phones that turned on
Peer chats in bighelp (`PUT /enrollments/<grantId>/preferences` with `{"version": 1, "peerChats": true}`);
it starts off. A question or approval in a peer chat still alerts, since it needs the person.

### Workflows

A workflow stage is a `hermes chat --source workflow` turn. Its replies, failures and Live Activity updates
never alert; a command it asks to run still asks for approval. The run alerts instead, read from the
workflow store's run journal by the notification worker:

- needs you (a sign-off waits, or the run needs attention): `clarification.required`
- succeeded and cancelled: `session.completed`; failed: `session.failed`

The alert's title is the workflow's name, and its chat is `workflow.run.<runId>` (the app opens the run).
Each kind has a switch per device (`PUT /enrollments/<grantId>/preferences` with `"workflows": {"needsYou",
"succeeded", "failed", "cancelled"}`); all start on. Capabilities list `preferences.workflows`.

## Instant alerts while bighelp is open

A push can take 10 seconds or more to arrive. While bighelp is open on a device, the device gets its alerts
straight from the host instead, in about one second. Locked and background devices get pushes as before.

1. While bighelp is in front (on a Mac, while it runs), the device holds a long request on
   `POST /api/plugins/loopdy/native/alerts/listen`. The host then records in the notification journal that this
   device is live. All Hermes processes use the same journal, so an alert from any process reaches the request.
2. When a turn queues an alert for a live device, the host gives it to that request. It is the same sealed
   alert that the push carries, so nothing new leaves the host unencrypted.
3. The device decides what to show. It shows nothing for the chat on screen, and it never tells the host which
   chat that is. It shows all other alerts like a push, then acks them on `POST …/native/alerts/ack`.
4. An ack in one second settles the alert, and the host never sends it to the notification service. Without an
   ack (the device went to the background, or the connection dropped), the host sends the push as before.
   Questions and approvals keep their grace period: the host offers them only when the push would start.
5. Both copies have the same event ID. When a push arrives for an alert that the device already showed, the
   device puts the push in Notification Center only and removes the first copy. You see one alert.

The device asks for this only when `/native/context` lists `native-live-alerts-v1`.

### Routes

All three routes need the native context headers (`If-Match`, `X-Loopdy-Request-ID`). Each names the device's
grants on this host: `grants` is a list of 1 to 8 `{"grantId", "recipientKeyId"}`. A grant counts only when it
is active and `recipientKeyId` is the ID of the content key the device registered for it.

`alerts/listen` takes `{"listenerId", "grants", "waitSeconds", "knownAvatars"}`:

- `listenerId` is a UUID that the device makes for each run of its listener.
- `waitSeconds` is 0 to 25.
- `knownAvatars` lists up to 32 SHA-256 hashes of agent pictures that the device has already.

The host answers when an alert is there, when the wait ends, or when a newer request from the same device takes
the grants. The answer is `{"alerts": [...], "grantIds": [...], "ackWindowMilliseconds": 1000}`:

- Each alert is `{"grantId", "agentId", "eventId", "eventType", "sessionReference", "turnId", "occurredAt",
  "sealed", "avatar"}`. `sealed` is the v2 envelope.
- `avatar` has the `sha256` of the encrypted picture. It also has the picture as `data` when the device does not
  have it and it fits.
- An answer holds at most 8 alerts and 1,000,000 bytes. The host gives each alert to a listener one time.

`alerts/ack` takes `{"grants", "eventIds"}` (1 to 32 event IDs). The answer is `{"settled": [...]}`: the alerts
that the host will not send as pushes. A push that started already is not in the list.

`alerts/stop` takes `{"listenerId", "grants"}`. The answer is `{"stopped": true}`. The device sends it when
bighelp goes to the background, so that the host sends its next alerts as pushes immediately.

Errors: `live_alerts_not_enrolled` (404) when no grant counts, `live_alerts_busy` (429) when 32 grants are live
on the host, and `live_alerts_unavailable` (503).

A device stays live for the wait it asked for plus 10 seconds, so that the next request can follow. After that,
or after `alerts/stop`, the host sends its alerts as pushes immediately.

### Quiet Hours

Each phone can set a daily window when this host sends it no alerts, such as 22:00 to 07:00. The phone keeps
the setting and sends it to every host that notifies it, with the phone's own IANA time zone. The window
belongs to that phone's grant, so one person's iPhone and iPad can have different windows.

- **When.** Just before the host sends an alert for a grant, it reads the time on that phone's clock in
  that time zone, so the window follows daylight saving time. The start is inside the window and the end is
  not. A window can cross midnight. Equal start and end times make an empty window.
- **What it skips.** Every alert in the window: replies, failures, scheduled tasks, helper results, questions
  and approvals. The alert never reaches the notification service or BuzzKit, and a device with bighelp open
  does not get it as an instant alert. A retry that falls in the window is skipped too.
- **Not sent later.** Quiet means quiet: a skipped alert is marked `quiet` and is never sent, also after the
  window ends. The reply is in the chat, and a question or approval waits there for the person.
- **Not affected.** Live Activity updates for a turn the person started, and the silent sign-in wake below.
- **Older apps** never send a window, so their alerts are unchanged.

The phone sets the window through the native API, when the host's `/native/context` lists
`native-notification-quiet-hours-v1`. The request needs the usual `If-Match` context ETag and a lowercase
`X-Loopdy-Request-ID`:

```http
POST /api/plugins/loopdy/native/notifications/quiet-hours
{"grantId": "<grant UUID>", "enabled": true, "startMinute": 1320, "endMinute": 420, "timeZone": "Europe/Berlin"}
```

`startMinute` and `endMinute` are minutes after local midnight (0 to 1439). The reply echoes the stored window
and tells whether it holds now:

```json
{"version": 1, "grantId": "<grant UUID>", "quietNow": true,
 "quietHours": {"enabled": true, "startMinute": 1320, "endMinute": 420, "timeZone": "Europe/Berlin"}}
```

Errors: `422 invalid_request` for a malformed body, `422 quiet_hours_time_zone_invalid` for a zone the host
can't read, `404 notification_enrollment_inactive` for a grant that isn't active here, and
`503 quiet_hours_unavailable` when the capability isn't listed. Removing the grant deletes its window.

## End-to-end encryption

With a registered content key, the host seals each alert's title, text and avatar for that phone
(P-256 key agreement, HKDF-SHA256, AES-256-GCM) and signs it with its notification key. The service and
BuzzKit only see a placeholder such as "New reply", the event type, the time and routing IDs. The phone's
notification extension checks the signature against the host key it pinned at setup, decrypts, and shows the
same title, text and picture as before.

A sealed alert must fit one push, so its envelope is capped at 2,300 bytes; very long text is shortened. The
format is in [`sealed_alerts.py`](../loopdy_plugin/sealed_alerts.py), and
`fixtures/contracts/sealed-alert-v2-vector.json` is shared with the app's tests.

The host only sends sealed alerts. Until a grant has a content key, nothing is queued for it, so an alert that
happens in the moment between turning notifications on and the phone registering its key is skipped. Apps older
than bighelp 2.3.0 build 20 never register a key and get no alerts. An unsealed alert left in the queue by an
older plugin is marked failed and never sent.

## Live Activities

The app starts the Live Activity itself. The host then sends updates for the current turn through the same
grant (`PUT/DELETE /notifications/enrollments/<grantId>/live-activities/<activityId>`). Updates carry only a
phase, a fixed label such as "Your agent is working" or "Your agent finished", and counts. They never include
message text.

## Host storage

The host's notification key and its grants, subscriptions, content keys and pending events live under
`<Hermes profile home>/plugin-data/loopdy/managed-notifications/`, owner-only. The key is created on first use
and stays put when the plugin is updated.
