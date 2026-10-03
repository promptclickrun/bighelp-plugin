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
- Failed runs always alert.

### Staying signed in

A rotating sign-in, like the Nous Portal's whose renewal token lasts a day, runs out if bighelp stays closed.
So every eight hours the background sender asks the service to wake each enrolled phone (one with a content
key) with a quiet push: `POST /v1/notifications/host-grants/<grantId>/wake` with
`{"version": 1, "reason": "renew-sign-in"}`. The push has no text, sound or topic, only
`bighelp_wake: {version: 1, type: "renew-sign-in", grantId}`, and the service sends at most one per grant every
six hours. The app then renews its sign-in to each computer it isn't connected to. A refused wake (an older
service answers 404) waits for the next interval and never removes the phone's enrollment.

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
