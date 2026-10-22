# Native hosted-room tool observations

This optional adapter consumes Hermes' public `on_room_member_activity`
observer. It uses no private session/task dictionaries, hidden-session
subscriptions, new WebSocket, scheduler or model execution loop.
The install does not need this hook. `plugin.yaml` declares only baseline hooks
in `provides_hooks`, so Hermes Plugin Doctor also accepts a host without it. On
such a host the plugin installs and works, and group chat activity stays off.

The enabled plugin registers only when the hook appears in public `VALID_HOOKS`;
`register_hook` accepting a name alone does not prove support. Unload retires the
callback lease, clears all feeds and fences already queued old callbacks. Native
context advertises `native-room-activity-v1` only with a live supported
registration. `registered_unobserved` remains honest until a valid tool callback
arrives; the plugin never changes the host's tool-progress configuration.

## Transport and exact DTOs

All operations are POST under `/api/plugins/loopdy/native/groups/activity/`,
using the existing real native Session, context `If-Match`, and canonical
`X-Loopdy-Request-ID` headers. Responses follow the native success/error header
rules in [Native workspace API](NATIVE_WORKSPACE_API.md).

| Suffix | Exact request | Successful result |
| --- | --- | --- |
| `open` | `{roomId}`, `{roomId,include:["member.responding"]}` or `{roomId,include:["member.responding"],peer:{homeInstallId,members:[{memberId,profile}]}}` | Empty `FeedPage`, cursor/highWater 0 |
| `poll` | `{roomId,streamId,after,limit}` | `FeedPage` |
| `close` | `{roomId,streamId}` | `{schemaVersion:1,roomId,streamId,closed:true}` |

```text
FeedPage = {
  schemaVersion:1, runtimeId, roomId, streamId,
  sourceState:"registered_unobserved"|"observed"|"unsupported_payload",
  upstreamLoss:"unobservable",
  openedAt:UnixMilliseconds, expiresAt:UnixMilliseconds,
  cursor:integer, highWater:integer, hasMore:boolean,
  resetRequired:boolean, resetReason:null|"buffer_loss"|"projection_loss",
  droppedTotal:integer, projectionDrops:integer,
  events:[{
    observationSequence:integer, observedAt:UnixMilliseconds,
    roomId, memberId, threadId, turnId, taskId, executionGeneration:integer,
    sourceSequence:integer|null,
    kind:"tool.started"|"tool.completed",
    tool:{id,name,durationMs:integer|null},
    arguments:{state,text}, result:{state,text}
  } | {
    observationSequence:integer, observedAt:UnixMilliseconds,
    roomId, memberId, threadId, turnId, taskId, executionGeneration:integer,
    sourceSequence:integer|null,
    kind:"member.responding"
  }]
}
```

`member.responding` events appear only in feeds opened with
`include:["member.responding"]`, which needs `native-room-typing-v1` in native
context (listed with `native-room-activity-v1`). Older apps never ask, so their
feeds keep exactly the tool events above. Hermes' `message.delta`,
`message.interim` and `reasoning.delta` mean the member is writing; the plugin
sends at most one such event per member turn (member, task, execution
generation) every 2 seconds, and never the text. The app shows "is typing"
from them and drops it when the member's message or terminal turn event reaches
the room log, or when the events stop.

A member's turn can run in any Hermes process on the computer (the gateway, a
`hermes serve` backend), while the feed is served by the dashboard. So the
hooks write a short note (coordinates and time, never text) to
`plugin-data/loopdy/room-presence.sqlite3` at the Hermes root, at most every 2
seconds per member turn, and each poll reads the notes newer than its last.
Notes older than 30 seconds are deleted.

### Agents in a room homed on another computer

With `native-room-peer-typing-v1` (Hermes has the `on_stream_delta` hook), a
feed can be opened for agents on this computer that are members of a room
whose home is another computer (RoomLink): `peer` names the home's
installation ID and each member's ID and profile. This computer must hold a
live RoomLink reservation for each profile (`probe_peer_room_reservation`),
and such a feed must ask for `member.responding`; it carries nothing else.
Those turns run here through the API server, which fires the agent's stream
hooks but not `on_room_member_activity`. The plugin matches the stream's
session to Hermes' hidden room session
(`room_` + sha256 of home install ID, room ID, member ID and profile, as in
`api_server_room_dispatch`). Its events use `threadId: "peer"`, the session ID
as `taskId` and generation 1, since this computer doesn't know the home's
thread or turn.

### Rooms on Hermes' session path

From Hermes 0.22.4 a gateway's own listener answers `groups.*` on its session
path. That path keeps rooms in the gateway authority's store and runs member
turns without `on_room_member_activity`, so its feeds carry `member.responding`
only, never tool events. When the dashboard is that listener (its app state has
a `session_authority`), the plugin reads the room from that authority's store
(`gateway.hosted_rooms.room_state`) instead of the older `groups.state`
handler, and it matches stream deltas (`surface: "bot_room"`) to each member's
hidden room session: titled `Group: <room ID>` with source `bot_room` in that
member's own `state.db`, read-only, plus the sessions compression continued it
in. A member's session appears on its first turn, so each poll reads them again
and a new one never retires the feed. Events use `threadId: "peer"`, the session
ID as `taskId` and generation 1, as above. A `hermes serve` dashboard keeps the
older path, unchanged. As on every path, an agent whose own profile doesn't load
this plugin reports no writing.

Every shown property is required. Detail `state` is exactly `available`,
`unavailable`, `omitted_size` or `omitted_sensitive`; `text` is a string only
when available, otherwise null. No inferred profile, success, current-running
or durable log-event ID is included.

Each stream is a server-minted, disposable view coordinate, bound to the actual
verified principal/context and room. It is not a credential. Before opening and
polling, the existing fixed native `groups.state` path validates current room
identity, authority epoch and immutable roster. Changes retire the feed. Native
workspace authority applies; this does not add a fictional per-person room ACL.

## Semantics, omissions and loss

Display started as **Started**, not proof the tool is still running. Display
completed as **Finished; outcome not reported**, not succeeded. Match rows by
runtime/feed, room/member/task/execution generation and native tool ID. Source
turn/thread coordinates remain exact. Canonical native room logs, final
messages, approvals and retry/cancellation remain authoritative.

The feed begins with callback receipt after opening, not source execution after
opening: an upstream event can already be queued. `observedAt` is plugin receipt
time. Nothing is retained before a viewer explicitly opens a feed; no source
text is written to disk.

Only `payload.args` and `payload.result` are optional detail sources. Missing,
non-JSON, invalid-control or oversized data is explicitly unavailable/omitted;
escaping can require omission to satisfy the encoded observation ceiling.
Existing known-credential patterns and credential-bearing object keys suppress
details. This is conservative screening, not universal secret detection.
Details never replace or enter durable history. Message and reasoning text,
approval payloads, duplicate preview text and tool-success inference are
excluded.

Hermes' upstream observer queue silently drops oldest entries and has no public
drop counter. `upstreamLoss` therefore ALWAYS says `unobservable`; gaps in its
session-wide sequence cannot measure loss. The local observation sequence and
drop counters describe only this plugin feed. Equal bounded source observations
with a source sequence are deduplicated while retained, not durably.

If the requested cursor predates discarded entries, or a room-bound callback
cannot safely project its required coordinates, return resetRequired with empty
events and cursor=highWater. Clear transient rows, reconcile native room
state/log and open a fresh feed. Do not invent the missing activity. A source
sequence is not a room-log cursor.

## Bounds, failures and lifecycle

- 16 feeds/process, 128 observations and 256 KiB encoded events/feed (4 MiB
  total), plus bounded digest/coordinate bookkeeping.
- 60-second inactivity lease; authenticated successful polls renew it.
- Poll limit 1..8, cursor 0..highWater; responses <=196608 UTF-8 bytes.
- Each detail <=8192 UTF-8 bytes and each observation <=20480 encoded bytes.
- Room/member/thread/turn/task IDs <=128 ASCII identifier characters; native
  tool ID <=512, name <=128; safe integers, finite duration <=24 hours.

Shared native errors remain 401/412/428/422. `activity_unavailable`503 means no
supported live observer, `activity_capacity`429 a full feed budget,
`activity_not_found`404 mismatched principal/room, `activity_reset_required`410
an expired/retired stream or changed room authority/roster. `room_unavailable`
is404 for an unavailable returned room or503 for an unverified native read;
malformed room data is `room_state_invalid`503. Errors never create a replacement
stream or reflect raw source content.

Request cancellation does not create a replay obligation: an abandoned open is
bounded by its idle lease, polls are reads, and a closed stream is not silently
reopened. Owner/context checks bracket awaits. As with ordinary native HTTP,
revocation cannot recall data already delivered.

Tests use actual stock serve auth and the public hosted-service lifecycle/emitter
in a synthetic home. No real provider call, message send, installed runtime
change, deployment or activation is performed.
