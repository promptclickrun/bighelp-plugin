# Security and privacy

What the bighelp plugin stores on your Hermes host, what leaves it, and who can see it. This describes the
current code. It isn't legal advice or an independent audit.

## Where data goes

| Data | Stays on your host | Leaves your host |
| --- | --- | --- |
| Chats, prompts and replies | In Hermes' normal history | To the app you're signed in with, and to your AI provider as Hermes normally sends it |
| Agent board (Feed, Ideas, Goals, Activity, approvals) | `plugin-data/loopdy/board.sqlite3` per profile | To the app on request |
| Agent attachments | Approved copies in `plugin-data/loopdy/agent-attachments.sqlite3` | To the app, by opaque ID |
| Workspace files | Only folders you grant | To the app, read on request |
| iPhone tool results | In the chat's normal history, like any tool result | Come from your phone; go to your AI provider with the chat |
| Live voice audio | Not stored | Flows directly between your phone and the voice provider |
| Notification content | Pending events under `plugin-data/loopdy/managed-notifications/` | Sealed for your phone with current app builds; see below |
| Agents talking to each other | Not stored: read from the other agent's own history | To the app on request: chat IDs, times and the tool running, matched by a hash of each message; never words. See [Agents talking](AGENT_TALK.md) |
| A chat busy in another process | Not stored: read from Hermes' chat leases and the agent's history | To the app on request: whether another Hermes process runs the chat, the tool and its start time. Stop sends that process Ctrl-C, only when its pid and start time match Hermes' lease. See [Agents talking](AGENT_TALK.md) |
| Teamwork | When on: a team brief (agents' names, IDs and roles) in each agent's `config.yaml` under `platform_hints.bighelp`, and `plugin-data/loopdy/team.json` at the Hermes root | Agents' names and roles to the app, and to your AI provider in each agent's instructions. See [Teamwork](TEAMWORK.md) |
| Provider usage | Cached in memory for five minutes | Keys go only to their own provider; usage numbers (no keys, emails or account IDs) go to the app. See [Provider usage](PROVIDER_USAGE.md) |
| Composio Connect | The MCP server entry in the agent's `config.yaml`; Hermes keeps its sign-in tokens | To the app: server names, URLs without query or user info, on or off, and whether Hermes has saved sign-in tokens. Never tokens, headers or keys. See [Composio Connect](COMPOSIO.md) |
| Host telemetry | Nothing on disk except a random ID and your on/off switches; the events wait in memory | Timings, counts and fixed codes under a random host ID (never text, paths, names or addresses) to bighelp's telemetry service, then Microsoft Azure for 90 days. On by default; see the switches in [Host telemetry](HOST_TELEMETRY.md) |

All plugin data lives under the active Hermes profile home in `plugin-data/loopdy/`, with owner-only
permissions. Nothing is sent to bighelp except notifications you turned on and, unless you turn it off, the
[host telemetry](HOST_TELEMETRY.md).

## Notifications

The notification service and BuzzKit deliver alerts but can't read them. The host encrypts each alert's title,
text and agent picture for the recipient phone and signs it with its own key. What the service does see: which
grant it belongs to, the event type (for example "reply" or "approval needed"), the time, and a placeholder text.
The host never sends an alert it can't seal: until the phone has registered its content key, which current app
builds do right after turning notifications on, no alert is queued or sent. App builds before bighelp 2.3.0 (20)
never register a key, so they get no alerts.

Live Activity updates contain only a phase, a fixed label and counts, never message text. The Lock Screen can
still show the agent and chat name the app put on the activity; turn off Live Activities or notification
previews in iOS settings if that matters to you.

## Cards

Cards are data, not code. Since app build 3 they are static: every value shown is embedded in the card, the
plugin and app both reject anything else, and opening a card makes no network request. See [Cards](CARDS.md).

## iPhone tools

Off until you enable each one for a host. They work only while bighelp is open on that chat, and results go
where any tool result goes: your host, your AI provider and the chat history. See
[iPhone tools](IPHONE_DEVICE_TOOLS.md).

## Logging

The plugin's log messages are short, fixed warnings (for example, why a feature isn't available). They are
written to leave out prompts, replies, attachments, notification text, credentials and keys.

## Limits

- Anyone who can sign in to your Hermes dashboard has the same access as the app.
- A compromised host can read everything the host can read. Encryption protects notifications in transit, not
  your host.
- Encryption hides content, not the fact that an alert was sent or when.
- No independent security audit has been done.
