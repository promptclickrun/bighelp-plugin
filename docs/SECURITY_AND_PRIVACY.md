# Security and privacy

What the bighelp plugin stores on your Hermes host, what leaves it, and who can see it. This describes the
current code. It isn't legal advice or an independent audit.

## Where data goes

| Data | Stays on your host | Leaves your host |
| --- | --- | --- |
| Chats, prompts and replies | In Hermes' normal history | To the app you're signed in with, and to your AI provider as Hermes normally sends it |
| Agent board (Feed, Ideas, Goals, Activity, approvals) | `plugin-data/loopdy/board.sqlite3` per profile | To the app on request |
| Agent attachments | Approved copies in `plugin-data/loopdy/agent-attachments.sqlite3` | To the app, by opaque ID |
| Workspace files and wikis | Only folders you grant | To the app, read on request (wikis can also be edited) |
| iPhone tool results | In the chat's normal history, like any tool result | Come from your phone; go to your AI provider with the chat |
| Live voice audio | Not stored | Flows directly between your phone and the voice provider |
| Notification content | Pending events under `plugin-data/loopdy/managed-notifications/` | Sealed for your phone with current app builds; see below |
| Context usage | Token counts only, no text | To the app |

All plugin data lives under the active Hermes profile home in `plugin-data/loopdy/`, with owner-only
permissions. Nothing is sent to bighelp except notifications you turned on.

## Notifications

The notification service and BuzzKit deliver alerts but, with current app builds, can't read them. The host
encrypts each alert's title, text and agent picture for the recipient phone and signs it with its own key. What
the service does see: which grant it belongs to, the event type (for example "reply" or "approval needed"), the
time, and a placeholder text. Phones on app builds before bighelp 2.3.0 (20) haven't registered a content key,
so their alerts are sent unsealed and are readable by the service.

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
