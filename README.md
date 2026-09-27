# bighelp plugin for Hermes

The Hermes plugin behind the [bighelp](https://github.com/promptclickrun/bighelp) iPhone app. It adds what the app
needs on top of a stock Hermes host: agent boards, native cards, iPhone tools, live voice, files and wikis, and
optional end-to-end encrypted notifications.

Hermes stays in charge of agents, chats, tools, approvals and scheduled tasks. The plugin runs inside Hermes'
own dashboard and uses its sign-in. It opens no extra ports and needs no bighelp account or public URL.

> The plugin's internal name is still `loopdy`, so commands are `hermes loopdy …` and routes live under
> `/api/plugins/loopdy/…`. Existing installs keep working.

## Requirements

- Hermes 0.21.1 or later on macOS or Linux. Windows works for core features, but file, wiki and self-update
  features need macOS or Linux. CI tests Hermes 0.21.1 and 0.21.2. The bighelp app supports hosts on Hermes
  0.21.2 to 0.21.5.
- The bighelp app connects straight to your Hermes dashboard, over your local network or Tailscale. It signs in
  with the host's own login.

## Install

```bash
hermes plugins install promptclickrun/bighelp-plugin --enable
```

Then restart the Hermes gateway and dashboard the way you normally do. The app can also install or update the
plugin from its host setup screen; it installs the exact version that app build was tested with.

To install a local checkout, commit your changes first (Hermes installs from Git), then run:

```bash
hermes plugins install "file://$PWD" --enable
```

## Update

```bash
hermes loopdy update            # install the latest release from main
hermes loopdy update --restart  # …and restart the gateway once
hermes loopdy update-status     # check the last update
```

The updater pins `main` to an exact commit, validates it, backs up the current copy and installs it with Hermes'
own installer. Your settings stay in place. It refuses to overwrite a modified install. Self-update needs macOS
launchd or Linux user systemd.

The bighelp app shows when a host's plugin is out of date (Settings › Hosts) and can update it, restart the
messaging gateway, and restart the Hermes process it's connected to so the new version loads. It then checks that
the new version is running. The in-place restart (`native-host-restart-v1`) keeps the same process, so launchd,
systemd and Hermes Desktop keep supervising it; chats running on that process stop.

## What it adds

| Feature | What you get in bighelp | Details |
| --- | --- | --- |
| Agent board | Each agent's Feed, Ideas, Goals, Activity and approval history, next to its chat. Agents post with the `bighelp_board` tool. | [Agent board](docs/AGENT_BOARD.md) |
| Apps tab | Files the agent recently made or changed, and the pictures and videos it delivered. | [Artifacts and media](docs/APPS_ARTIFACTS_AND_MEDIA.md) |
| Cards | Native cards in chat: summaries, metrics, lists, timelines, charts, forms, checklists, weather, scores, stock quotes and more, via the `loopdy_render_*` tools. Card templates can be saved and reused. | [Cards guide](docs/CARDS.md) |
| Reactions | Agents can react to your messages with an emoji (`loopdy_react_to_message`). | |
| Secure input | An agent can ask for a password, API key or other secret with `bighelp_request_secure_input`. bighelp shows a masked pop-up; Hermes saves what you type to the agent's `.env` and the agent can use it only as `$NAME` in commands. The AI never sees the value. Hermes' own AI provider keys are excluded. | |
| Agent templates | Save an agent's setup as a template and start new agents from it. | |
| iPhone tools | `iphone_health` (read-only), `iphone_calendar` and `iphone_reminders`. Each is off until you allow it for a host on your phone. They only work while bighelp is open and in the foreground. | [iPhone tools](docs/IPHONE_DEVICE_TOOLS.md) |
| Live voice | Talk with your agent in real time. It uses the Codex subscription signed in on the host (or an API key); the work itself runs as a normal Hermes chat. | [Live voice](docs/LIVE_VOICE.md) |
| Files and projects | Browse workspace folders you grant, and see a project's Git status and diffs. Read-only. | [Workspace files](docs/WORKSPACE_FILES.md), [Project Git](docs/NATIVE_PROJECT_GIT.md) |
| Wiki | Connect a folder of Markdown notes and read or edit it from the app, using your Hermes login. | [Wiki](docs/NATIVE_WIKI.md) |
| Group chat activity | See which tools each agent in a group chat is using, live. Needs a Hermes host with the `on_room_member_activity` hook. | [Room activity](docs/NATIVE_ROOM_ACTIVITY.md) |
| Context usage | How much of the model's context the last request used. | [Context usage](docs/CONTEXT_USAGE.md) |
| Provider usage | How much of each AI plan or balance you've used: Claude, Codex, GitHub Copilot, OpenCode, OpenRouter, DeepSeek, Nous Portal and more. Found automatically from the tools installed on the computer and the providers set up in Hermes. Read-only. | [Provider usage](docs/PROVIDER_USAGE.md) |
| Generated media and files | Agent-made images, videos and files show up in chat. Hermes' own media rules decide what can be shared, and host paths are never exposed. | [Agent attachments](docs/AGENT_ATTACHMENTS.md) |
| Model names | `model-names.json` gives friendly model names. The app can refresh it without a plugin update. | |

### Cards: rules for agents

- Call a card tool directly when it's visible in the current tool list. If Hermes has hidden it, use the
  official progressive-disclosure bridge (`tool_search`, `tool_describe`, `tool_call`) to run that exact tool.
  Never substitute a different tool.
- **Inline card in the active chat:** return the card as part of the current reply.
  Do not use the notification channel just to answer the current chat.
- **Proactive or scheduled card** (shown later in the app): target the `loopdy` platform and make the exact
  result the card tool returned the complete final message, with no extra text. Hermes does not auto-forward an
  earlier card to a later channel send. Never script or reconstruct a card.
- Cards are display-only. Everything shown is embedded in the card, and opening one makes no network request.

The `skills/` folder ships read-only guidance for agents, for example `skill_view("loopdy:generative-ui")`:

- `generative-ui`: when and how to use each card tool.
- `bighelp-feed-and-ideas`: posting to the agent board only when the user asks.
- `custom-theme-authoring`: making themes the app can import.

## Notifications (optional)

Notifications and Live Activities are opt-in per host from the app. Chat never depends on them.

- **Delivery.** [BuzzKit](https://buzzkit.dev) delivers to your phone. BuzzKit's sending key lives with bighelp's
  notification service, never on your host. Your host holds only a grant for one phone, one agent profile, the
  event types you chose and an expiry date.
- **What you get.** Replies, failed chats, finished scheduled tasks, approvals, questions from your agent and
  helper results. Each shows with the agent's name and picture. Tapping one opens the chat after the app checks
  it with your host.
- **End-to-end encrypted (2.16.0 and later).** The app gives its own key straight to your host. The host encrypts
  each notification's title, text and avatar for that phone and signs it. The notification service and BuzzKit
  only pass along bytes they can't read. They see the event type, the time and routing IDs. The format is in
  [`sealed_alerts.py`](loopdy_plugin/sealed_alerts.py).
- **Live Activities.** Lock Screen and Dynamic Island updates carry only a phase, a fixed label (such as
  "Your agent is working") and counts. They never include message text.

More detail: [Notifications](docs/NOTIFICATIONS.md).

## Approvals

The app uses Hermes' own approvals. It offers exactly the choices Hermes gives (once, this session, always, or
deny) and never invents one. No configuration is needed.

## Commands

```text
hermes loopdy status                     Plugin and notification status (never prints keys or tokens)
hermes loopdy update [--restart]         Update from main
hermes loopdy update-status              Last update result
hermes loopdy files grant|revoke|roots   Manage read-only workspace folders
hermes loopdy files list|read|status|diff
hermes loopdy wiki …                     Host-side wiki grants
```

## Legacy code

Earlier versions paired hosts through a bighelp Link account and sent notifications through a relay or direct
APNs. The app no longer uses any of that. The related code, settings and commands (`hermes loopdy link …`,
`provider`, `configure-apns`, `direct …`) remain only so existing data and compatibility tests keep working.
Don't set them up on new hosts.

## Development

Run the tests with a throwaway Hermes home and a temporary folder outside any Git checkout. On macOS use
`/private/tmp`, not `/tmp`.

```bash
HERMES_HOME=/private/tmp/bighelp-test-home TMPDIR=/private/tmp \
PYTHONPATH=/path/to/hermes-agent:"$PWD":"$PWD/tests" \
  /path/to/hermes-agent/venv/bin/python -m unittest discover -s tests -v

hermes plugins doctor . --ci
```

`fixtures/contracts/` holds test vectors shared with the app, including the encrypted notification vector.
CI runs the compatibility tests against each supported Hermes version.

## License

Apache-2.0. The card format's constrained JSON-tree approach credits Sameer Gupta's
[Generative UI DSL](https://github.com/sameergdogg/generative-ui).
