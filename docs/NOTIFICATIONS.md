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
- **The app** shows the alert. When you tap it, the app goes to the alert's host and opens the chat at once.
  The alert names its agent and chat, and the phone finds the host from the saved grant. If the phone cannot
  match the alert to a host and chat this way, the app first asks the host about the event
  (`GET /notifications/enrollments/<grantId>/events/<eventId>`).

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

A grant is for one agent, not for one chat. All chats of that agent can send alerts of the grant's event types,
also chats that you did not open on this phone. Approvals are different: the host sends an approval push only for
a chat that the phone subscribed. When the phone unsubscribes a chat, the host deletes the alerts for that chat
that it did not send yet.

Turning notifications off revokes the grant with the service, so the host can no longer send alerts for it.
Removing the grant on the host (`DELETE /notifications/enrollments/<grantId>`) also deletes the host's local
copy, including the phone's content key.

## Events

| Event | When |
| --- | --- |
| `session.completed` / `session.failed` | The agent finished a reply, or couldn't. When its only answer was a reaction to your message, the alert reads "Reacted 👍 to your message". |
| `scheduled.completed` / `scheduled.failed` | A scheduled task finished or failed. |
| `approval.required` | The agent needs your approval to continue. |
| `clarification.required` | The agent asked you a question. |
| `subagent.completed` / `subagent.failed` | A helper the agent started finished or failed. |

Each alert shows the agent's name, picture and the start of its message (up to 1,600 characters). Event IDs
are stable (`<grantId>:<sha256>`), so a retry never produces a second alert.

The picture is the agent's picture in Hermes (`profiles.get_asset`). The notification service does not accept
an alert without a picture, and Hermes has no default picture. So, when an agent has no picture, or a picture
that the host cannot use (larger than 512 KB, empty, or not readable), the alert shows bighelp's own picture.
The alert still goes out. This applies to all alerts: replies, failures, scheduled tasks, helpers, questions,
approvals, workflows and messages from other agents. Each Hermes process logs one warning for each agent and
reason. The warning has no names or paths. The default picture is
[`loopdy_plugin/assets/default-agent-avatar.png`](../loopdy_plugin/assets/default-agent-avatar.png): the bighelp
app icon art, 256 × 256 pixels, from the [bighelp app repository](https://github.com/promptclickrun/bighelp).

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
canonical "Bot Chat" session. A turn that another agent started is a peer chat turn: its reply, failure and
helper results alert only phones that turned on Peer chats in bighelp
(`PUT /enrollments/<grantId>/preferences` with `{"version": 1, "peerChats": true}`); it starts off. A question
or approval in a peer chat still alerts, since it needs the person.

The Bot Chat is also where a person talks to the agent directly (Bots mode opens it). So the host looks at
the message that started the turn: when it carries Hermes' agent signature (`Message from <name> (@<agent>):`)
the turn is a peer chat turn, and when it doesn't, the turn is an ordinary chat and alerts as one, in the Bot
Chat as in any chat. Hermes gives each turn's message to the plugin's hooks, so no history is read. If the
host did not see the turn's message (for example the host restarted during the turn), the Bot Chat counts as
a peer chat, as it did before, and no other chat does.

The same switch covers an agent's message to another agent in any chat, for example the answer to work
one agent handed to another with `message_agent`. Hermes saves it in the receiving agent's chat, signed
`Message from 🤖 Nova (@nova):`. When the receiving agent's next turn starts, the plugin reads that turn's
message and the messages saved since the agent last spoke. A message signed by another agent on this
computer (`@hermes` is the default agent) alerts as a `session.completed` in the receiving agent's chat,
with the sending agent's name and picture (bighelp's picture if it has none). Each message alerts once (its
event key is the SHA-256 of the text). A handle that isn't an agent on this computer, a relayed `@name@peer`,
or the agent's own handle never alerts.

### Workflows

A workflow stage is a `hermes chat --source workflow` turn (`--source tool` where Hermes runs turns in its
gateway; the plugin then knows the stage's session by its brief). Its replies, failures and Live Activity updates
never alert; a command it asks to run still asks for approval. The run alerts instead, read from the
workflow store's run journal by the notification worker:

- needs you (a sign-off waits, or the run needs attention): `clarification.required`
- succeeded and cancelled: `session.completed`; failed: `session.failed`

The alert's title is the workflow's name, and its chat is `workflow.run.<runId>` (the app opens the run).
Each kind has a switch per device (`PUT /enrollments/<grantId>/preferences` with `"workflows": {"needsYou",
"succeeded", "failed", "cancelled"}`); all start on. Capabilities list `preferences.workflows`.

### Scheduled tasks: where a tap goes

A scheduled task runs in a session of its own (`cron_<job>_<time>`, or `local-<sha256>` in Hermes' unified
runtime). Nobody opened that session, so an alert that names it cannot take the phone to a chat. The host now
remembers the chat where each job was made, and the alert can name that chat or the job:

1. **The job's chat.** When an agent makes or changes a job with Hermes' cron tool (`cronjob_manage`, or `cronjob`
   on older Hermes) during a turn of a person's chat, the host records the job and that chat
   (`scheduled_chats` in the notification journal: agent, job ID, chat session ID, time). It reads only Hermes'
   own answer to the tool call, so a refused call records nothing. A later change from another chat moves the
   job to that chat. A removal with the tool deletes the record. A job made by a scheduled run, a workflow stage,
   a helper, a group chat turn or another agent's session gets no chat. The host keeps at most 512 records per
   agent; the oldest go first.
2. **The alert.** When a run of that job alerts, the host checks that the chat still exists and still belongs to
   the same agent. If it does, the alert names `scheduled.chat.<session ID>`. If the chat is gone, the record is
   deleted and the alert names the job, `scheduled.job.<job ID>`; the app opens the job in Scheduled tasks. A job
   made in a terminal or in the app's Scheduled tasks screen also names the job. Like any chat, the name is hashed
   with the agent into the alert's `sessionReference`.
3. **The run stays the alert's identity.** The event ID and the event's `sessionId` still name the run, so a
   retry is still one alert. A run that names no job (Hermes before `ctx.current_cron_execution()` with a
   `local-` session ID) names the run, as before.

Only phones that ask get these names. The host lists `preferences.scheduledLinks` in its capabilities, and the
app sends `"scheduledLinks": true` in `PUT /enrollments/<grantId>/preferences`. It starts off. An older app never
sends it, so its scheduled alerts still name the run, as they did. The separate names also keep a scheduled alert
apart from the replies of the chat it names: a phone looking at that chat still shows it, and opening the chat
does not clear it, because the run's text is not in the chat.

### Channels

A channel is where a person talks with an agent: bighelp itself, iMessage, Telegram, a terminal, and so on. Each
phone picks the channels it gets alerts from, so it can get only bighelp's chats, or every channel's
([`notification_channels.py`](../loopdy_plugin/notification_channels.py)).

- **An alert's channel.** A reply, failure, helper result, question or approval comes from its session's stored
  `source`. `bighelp`, older bighelp builds' `loopdy` and `ios`, and turns nobody reads anywhere else (Group Chat
  members, workflow stages, helpers, Kanban workers) are bighelp, and so is the agents' Bot Chat whatever its
  source (Hermes' CLI stores it as `cli`; Peer chats has its own switch). `cli`, `tui`, `oneshot` and `tool` are
  `terminal`. Any other source is its own channel, by its Hermes platform name (`imsg`, `telegram`, ...).
- **Scheduled tasks.** Hermes runs every job as `cron`, which no person reads. So a run comes from where its job
  delivers, read from `cron/jobs.json` by Hermes' own rules: `local`, `bot-chat` and the retired `loopdy` inbox
  are bighelp; `origin` is the platform of the chat the job was made in (an origin Hermes skips, without a chat
  or on the API server, is bighelp); `all` is every channel but bighelp. A failed run comes from `deliver` and
  `failure_deliver` both: the run stays where `deliver` keeps it. A run whose job can't be found is bighelp.
- **Rules.** A phone sends `"channels": {"other": <bool>, "choices": {"<channel>": <bool>, ...}}` in
  `PUT /enrollments/<grantId>/preferences` (at most 64 choices, each a channel an alert can carry: never `bighelp`,
  and never a source that maps to another channel, like `cli` or `loopdy`). A channel in `choices`
  follows its choice; any other channel follows `other`. An alert with more than one channel (a job that
  delivers to two) goes out when any of them is on. bighelp always alerts; its own kinds have their own switches.
  Every phone starts with every channel on (`{"other": true, "choices": {}}`), and the host reads no session
  for an alert until a phone here turns a channel off.
- **Unknown stays on.** An alert whose session can't be read still goes out, as before.
- **The list.** `GET /enrollments/<grantId>/channels` answers `{"version": 1, "profile", "channels": [{"id",
  "name"}]}`: the channels the phone's agent has sessions on, plus where its jobs deliver, by name. bighelp isn't
  listed.

Capabilities list `preferences.channels`. Workflow alerts don't follow channels; they're bighelp's.

### Kinds of alert a phone can turn off

Each phone can turn off three kinds of alert. All start on. A phone sends them in
`PUT /enrollments/<grantId>/preferences` (the rest of that body is unchanged):

```json
{"version": 1, "peerChats": false,
 "kinds": {"scheduledTasks": true, "deliveries": true, "groupChats": true}}
```

`kinds` is optional. Left out, the phone's choices stay as they are. When it is there it holds all three keys,
each a real boolean, and no other key; anything else is refused with 422 (`notification_preferences_invalid`)
and changes nothing. The answer of that route, and `GET /enrollments/<grantId>/preferences`, hold
`"kinds": {...}` with the stored values. `GET /capabilities` lists `preferences.kinds: true`.

- **`scheduledTasks`** off: no `scheduled.completed` or `scheduled.failed` alert from a scheduled task's run.
- **`deliveries`** off: no alert from `bighelp_send_notification` (see Notify now).
- **`groupChats`** off: no alert from a Group Chat (a session of the `bot_room` source): no reply, failure or
  helper result. Questions and approvals (`clarification.required`, `approval.required`) always alert, because
  they need the person. A session that can't be read counts as an ordinary chat, so it alerts.

The switches work for each phone on its own, so one phone can turn a kind off while another keeps it. Quiet
Hours and Channels still apply to what is left.

### Notify now

An agent can notify the person without a scheduled task with the `bighelp_send_notification` tool: a long job
that finished while they were away, or a reminder they asked for. A normal reply already notifies them, so the
agent's brief says to use it only when they aren't in the chat.

- **Arguments:** `title` (1 to 80 characters after trimming) and `message` (1 to 1,000 characters after
  trimming), and nothing else.
- **The alert:** sealed and sent like every other alert, to the phones enrolled for the calling agent. It is a
  `scheduled.completed` event (every grant has it, and the notification service files it under "Scheduled tasks
  and deliveries"), marked as a delivery on the host: the Deliveries switch filters it, and Scheduled tasks does
  not. The alert's title is the agent's name. Its text is `<title> — <message>`, with the usual text rules (one
  line, at most 1,600 characters).
- **Where a tap goes:** the chat that the tool ran in. A phone that asked for scheduled links
  (`scheduledLinks`) gets the name `scheduled.chat.<session>`, as for a scheduled task made in a chat. Other
  phones get the chat's own name, so a phone that shows that chat stays quiet.
- **Limit:** 10 an hour for each agent. A notification that no phone got doesn't count. Quiet Hours apply as for
  any alert.
- **Result:** `{"sent": <phones alerted>}`, or `{"sent": 0, "reason": "<plain words>"}` when no phone has
  notifications on for this agent, every phone turned off Deliveries, the hourly limit is reached, or the
  arguments were not valid.

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

The device asks for this only when `/native/context` lists `native-live-alerts-v1`. The host lists it only when it
can open its notification journal; otherwise it says why in `unavailable` and sends pushes.

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
on the host, and `live_alerts_unavailable` (503). The 503 hides the cause from the app. The host log says it
(`bighelp live alerts unavailable: <code>`) and host telemetry reports it; see [When the journal can't be
opened](#when-the-journal-cant-be-opened).

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
phase, a fixed label such as "Your agent is working" or "Your agent finished", and counts. The notification
service and Apple can read these. They never include message text.

### Sealed card detail

An app that shows more on the card gives the host a key for that chat's row
(`PUT /notifications/enrollments/<grantId>/live-activities/<activityId>/card-key`, body
`{"version": 1, "key": "<32 bytes, base64url>"}`). The key comes over the app's own connection to this host,
never through the notification service. `capabilities` lists `sealedCardDetail: {"version": 1}`.

From then on each update for that card also carries `sealed`: the step in progress and what it works on, the
last three steps, the model's latest words between steps, a question or command that waits for the person, the
reply's first lines when the turn ends, helper goals, context use and the model's name. It is AES-256-GCM under
the row key, bound to the update's timestamp, and at most 1,200 characters. The service keeps and forwards the
bytes and cannot read them. A note that could hold a credential is left out. The format is in
[`card_detail.py`](../loopdy_plugin/card_detail.py), and `fixtures/contracts/sealed-card-v1-vector.json` is
shared with the app's tests.

A card with detail gets an update for each new step, at most one every 8 seconds. A new step alone is sent
with `quiet: true`, which the service sends at low priority, except that one update in every 30 seconds is not
quiet. A question, a command that waits for approval and the end of the turn go at once. A chat without a key
gets the updates it always got, 30 seconds apart. If the service refuses an update that has `sealed`, the host
drops that card's key and sends plain updates.

While the agent asks a question with `clarify`, or a command waits for approval, the card's phase is `waiting`.

A card goes stale on the phone 120 seconds after its last update. While a turn runs and no hook fires (a question
that waits, one long tool call), each Hermes process sends that turn's state again once a minute. It stops 30
minutes after the turn's last hook, unless the turn waits for the person.

## Host storage

The host's notification key and its grants, subscriptions, content keys and pending events live under
`<Hermes profile home>/plugin-data/loopdy/managed-notifications/`, owner-only. The key is created on first use
and stays put when the plugin is updated.

### When the journal can't be opened

The folder must be owned by the user that runs both the gateway and the dashboard. Its mode is 700 and its files
(`journal.sqlite3`, `identity.lock`, `host-key.pem`) are 600, all owned by that user. The folder can't be a
symlink. A folder that this user owns and that is too wide is made private when the journal opens; a file is
never changed. The usual cause is a dashboard and a gateway that run as different users, or files that something else
copied or created.

When it can't be opened, the plugin names one fixed code (never a path) in the host log, in
`/native/context` and in host telemetry:

| Code | Meaning |
|---|---|
| `notification_private_storage_required` | The folder is a symlink or owned by another user, or one of the three files isn't 600 or isn't owned by this user. |
| `notification_identity_invalid` | `host-key.pem` is over 4,096 bytes or isn't a P-256 key. |
| `notification_platform_unavailable` | This computer has no `fcntl` (not a POSIX host). |

The dashboard looks at the same journal that the routes open for the request. A busy or briefly unreadable
database is not reported: the capabilities stay listed, and the route answers as it always did.

`/native/context` then leaves out `native-live-alerts-v1` and `native-notification-quiet-hours-v1` and gives the
code for each in `unavailable`. The app does not listen for instant alerts without the first one, so the host sends
every alert as a push until the journal is fixed. Enrollment and preferences answer 503 with the same code.

To fix it, as the user that runs the gateway and the dashboard:

```bash
chown -R "$(id -un)" "$HERMES_HOME/plugin-data/loopdy/managed-notifications"
chmod 700 "$HERMES_HOME/plugin-data/loopdy/managed-notifications"
chmod 600 "$HERMES_HOME/plugin-data/loopdy/managed-notifications/"*
```

If the folder is a symlink, replace it with a real folder (move the files into it). Then restart the gateway and the
dashboard, so that both open the journal again. A gateway that couldn't open the journal when it started (its log
says `Managed notification producer unavailable: <code>`) queues no alerts until it restarts, and a dashboard keeps
the old code in memory.
