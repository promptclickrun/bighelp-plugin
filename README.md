# bighelp plugin for Hermes

This Hermes plugin gives the [bighelp](https://github.com/promptclickrun/bighelp) app its extra features. The app
runs on iPhone, iPad, Mac and Vision Pro. On a stock Hermes host, the plugin adds agent boards, native cards, iPhone
tools, live voice, files, workflows and optional end-to-end encrypted notifications.

Hermes continues to control agents, chats, tools, approvals and scheduled tasks. The plugin runs inside the Hermes
dashboard and uses its sign-in. It opens no extra ports and needs no bighelp account or public URL.

> Hermes still knows the plugin by its first name, `loopdy`. That name is its folder, its routes under
> `/api/plugins/loopdy/…` and the `loopdy:` prefix on its skills. If we change these names, app builds that are
> already on phones stop working. Everything that agents and people read says bighelp. The older names
> (`hermes loopdy …`, the `loopdy` toolset, `LOOPDY_*` settings) continue to work.

## Requirements

- Hermes 0.21.1 or later. Hosts on Hermes 0.22 also work. CI runs compatibility tests against Hermes 0.21.1 and
  0.21.2 only.
- The bighelp app does not check the Hermes version. It reads the list of features that the host has
  (`/native/context`). If the host does not have a feature, the app tells you, for example "update the plugin".
- macOS or Linux. The plugin loads on native Windows, but these features need macOS or Linux:
  - notifications, Live Activities, instant alerts and Quiet Hours
  - workflows and Dev Factory
  - the Files tab, wikis and project Git
  - My Templates
  - provider sign-in
  - self-update and the in-place restart
- The app connects to the address of your Hermes dashboard, for example on your local network, through Tailscale
  or through a proxy. It signs in with the login of the host.

## Install

The bighelp app can install the plugin when you add a host. The app installs the latest release.

To install from a terminal, run:

```bash
hermes plugins install promptclickrun/bighelp-plugin --enable
```

This command installs the newest commit on `main`, not a release. If the app does not find the plugin, restart the
Hermes gateway and dashboard.

To install a local checkout, commit your changes first, because Hermes installs from Git. Then run:

```bash
hermes plugins install "file://$PWD" --enable
```

`hermes bighelp update` does not update an install from a local checkout.

## Update

```bash
hermes bighelp update            # install the latest release
hermes bighelp update --restart  # …and restart the gateway once
hermes bighelp update-status     # check the last update
```

`hermes bighelp update` installs only [GitHub Releases](https://github.com/promptclickrun/bighelp-plugin/releases).
The maintainer releases merged changes together from time to time (`.github/workflows/release.yml`). Thus a merge
alone does not get to hosts.

The updater does these steps:

1. It finds the latest release and pins its exact commit.
2. It validates the release.
3. It makes a backup of the current copy.
4. It installs the release with the Hermes installer.

The updater installs only a version that is newer than the installed version. It does not overwrite a modified
install, and your settings stay as they are. It needs launchd on macOS or user systemd on Linux. `--restart`
restarts the gateway once. The dashboard keeps the old code until you restart it too.

The bighelp app shows when the plugin on a host is out of date (Settings › System), and it can update the plugin.
For this, the app uses the Hermes plugin installer, not `hermes bighelp update`. Then the app can restart the
messaging gateway and the Hermes process that it connects to, so that the new version loads. After that, the app
makes sure that the new version runs. The in-place restart (`native-host-restart-v1`) keeps the same process, so
launchd, systemd and Hermes Desktop continue to supervise it. Chats that run on that process stop.

## What it adds

| Feature | What you get in bighelp | Details |
| --- | --- | --- |
| Agent board | Each agent's Feed, Ideas, Goals, Activity and approval history, next to its chat. Agents post with the `bighelp_board` tool. Feed posts can carry files that you can open, save and share. | [Agent board](docs/AGENT_BOARD.md) |
| Files tab | An agent's working folder, the files that the agent made or changed recently, and the pictures and videos that it sent. Read-only. In Simple Bot mode, each bot's Files tab shows the folder of that bot. Other screens show the folder of the agent that serves the app. | [Files tab](docs/APPS_ARTIFACTS_AND_MEDIA.md) |
| Cards | Native cards in chat: summaries, metrics, lists, timelines, charts, forms, checklists, weather, scores, stock quotes and more, through the `bighelp_render_*` tools. Agents can save a layout as a private card template and use it again with new values. | [Cards guide](docs/CARDS.md) |
| Reactions | Agents can react to your messages with an emoji (`bighelp_react_to_message`). | |
| Who's talking | When several people use bighelp with one host, agents see the name that each person saved in the app. Agents can ask for the names with `bighelp_people`. Names are labels, not logins. | [Who's talking](docs/PEOPLE.md) |
| Secure input | An agent can ask for a password, API key or other secret with `bighelp_request_secure_input`. bighelp shows a masked pop-up. Hermes saves what you type to the agent's `.env`. The value does not go into the chat, the tool result or a log. The agent is told to use it only as `$NAME` in commands and never to show it. The plugin refuses the AI provider keys of Hermes. | |
| My Templates | In Simple Bot mode, save a bot as a template (Share on the profile of the bot). The plugin keeps it on this host, and it shows as "My Templates" in the template picker of the app on every phone. The plugin refuses keys and passwords, and it skips a broken file but keeps the rest of the list. | [My Templates](docs/MY_TEMPLATES.md) |
| Template Catalog for agents | Agents can search bighelp's Template Catalog and fill in a template with you (`bighelp_templates_*`, in the `bighelp_templates` toolset). An agent can make a new agent from a template only after you turn on `templates_allow_create_agent`, and you approve each new agent. | [Template tools](docs/TEMPLATE_TOOLS.md) |
| iPhone tools | `iphone_calendar`, `iphone_reminders` and `iphone_location` (where you are now). Each tool is off until you allow it for a host on your phone. When you ask, `iphone_appearance` changes the light or dark mode and the chat color of bighelp. It is on until you turn it off. The tools work only while the phone is unlocked and bighelp shows a chat with that host. Only the agent that serves the app can use them. Current app builds do not offer Health, so only older builds can use `iphone_health`. | [iPhone tools](docs/IPHONE_DEVICE_TOOLS.md) |
| Live voice | Talk with your agent in real time. Live voice uses the Codex subscription that is signed in on the host, or an API key. The work itself runs as a normal Hermes chat. | [Live voice](docs/LIVE_VOICE.md) |
| Project Git | See the Git status and diffs of a project. Read-only. | [Project Git](docs/NATIVE_PROJECT_GIT.md) |
| Wiki | Routes that connect a folder of Markdown notes, so that you can read and edit it with your Hermes login. The current bighelp app does not show wikis. | [Wiki](docs/NATIVE_WIKI.md) |
| Agents talking | When one agent asks another (`hermes -p <agent> chat -q`, or `message_agent`), the chat shows what the other agent does while it works, and which of its chats the exchange used. The plugin reads the history of the other agent and does not change it. The app sends a hash of each message, never the words. When a chat is busy in another Hermes process, the app shows it and can stop that run. | [Agents talking](docs/AGENT_TALK.md) |
| Teamwork | Off until you turn it on in the app. Every agent learns who the other agents are and what they do, and its instructions tell it to give them the work that fits their role. The plugin saves this in the Hermes settings of each agent, and removes it when you turn Teamwork off. | [Teamwork](docs/TEAMWORK.md) |
| Group chat activity | See which agents in a group chat type and which tools they use, live. Needs a Hermes host with the `on_room_member_activity` hook. | [Room activity](docs/NATIVE_ROOM_ACTIVITY.md) |
| Chats that stay open | Your phone drops its connection each time you leave the app. Hermes then keeps bighelp chats open and ready to continue, as it does for Hermes Desktop. Without the plugin, Hermes ends them. | [Native workspace API](docs/NATIVE_WORKSPACE_API.md) |
| Provider usage | How much of each AI plan or balance you used: Claude, Codex, GitHub Copilot, OpenCode, OpenRouter, DeepSeek, Nous Portal and more. The plugin finds them from the tools installed on the host and the providers set in Hermes. Read-only. | [Provider usage](docs/PROVIDER_USAGE.md) |
| Workflows | Steps that your agents work through, such as research, draft and review, with your sign-off at the end. Stages can run one after another, or 2 to 5 at the same time. A run starts when you tap Run, when an agent starts it with `bighelp_workflows`, or on a schedule that you approve. Runs continue on the host when the app is closed. | [Workflows](docs/WORKFLOWS.md) |
| Dev Factory | Coding workflows on your Git projects: one run for each project, in a new worktree or in the project folder. An agent changes the code, the tests run (an agent fixes what they find), you examine the changes, and a pull request opens with your own GitHub sign-in on the host (the GitHub CLI, `gh`). | [Dev Factory](docs/DEV_FACTORY.md) |
| Usage activity | When your agents work (hours of the day), their messages, and the tokens and cost of each model for each day, for the Usage page of the app. The analytics of Hermes give the rest. Read-only. | [Usage activity](docs/USAGE_ACTIVITY.md) |
| Composio Connect | Lets your agents use the apps that you connect in your Composio account. bighelp can add and turn on the Composio Connect MCP server in Hermes, and shows if Hermes is signed in to it. The MCP sign-in of Hermes keeps the tokens. | [Composio Connect](docs/COMPOSIO.md) |
| Provider sign-in | Sign in from the phone to accounts that otherwise need a terminal on the host (GitHub Copilot, Claude Code, Claude subscriptions). The sign-in tool of the provider runs on the host. The phone gets only its link and code. | [Provider sign-in](docs/PROVIDER_SIGN_IN.md) |
| Generated media and files | Images, videos and files that agents make show in chat. The media rules of Hermes decide what the plugin can share, and the plugin never shows host paths. | [Agent attachments](docs/AGENT_ATTACHMENTS.md) |
| Model names | `model-names.json` gives friendly model names. The app can get a new copy from `main` without a plugin update. | |

Hermes loads the plugin only into the agents that have it, usually only the default agent. Other agents cannot use
the `bighelp_*` and `iphone_*` tools. The agent that serves the app is the agent of the Hermes dashboard that the
app connects to (`servingProfileId` in `/native/context`), usually the default agent.

### Cards: rules for agents

- Call a card tool directly when it is visible in the current tool list. If Hermes hides it, use the
  official progressive-disclosure bridge (`tool_search`, `tool_describe`, `tool_call`) to run that exact tool.
  Never use a different tool instead.
- **Inline card in the active chat:** return the card as part of the current reply.
  Do not use the notification channel just to answer the current chat.
- **Proactive or scheduled card** (shown later in the app): a scheduled job (with `deliver: "local"`) calls the
  card tool. The job puts the `display_markdown` of the card in the final reply of the run. bighelp notifies the
  user with that reply. Each card shows as a short plain preview: the optional `notification_text` of the tool, or
  a preview made from the card. A card tool result does not auto-forward to a later message, and a card never
  notifies anyone by itself. Never script or reconstruct a card.
- Cards are display-only. The card contains everything that it shows, and a card that you open makes no network
  request.

### What agents are told

The app starts and opens its chats again with session source `bighelp`. For those chats, the plugin adds a short
brief to the instructions of the agent. The brief tells the agent these things:

- that it is in the bighelp app
- what the chat shows
- which bighelp tools to use
- how scheduled jobs get to the phone

Other chats with a person get a short note about the app and the `bighelp` skill. Scheduled runs, helpers and other
runs without a person get nothing.

The `skills/` folder has read-only guidance for agents. Agents open it with `skill_view("loopdy:<name>")`:

- `bighelp`: start here. Cards, forms, Feed, Ideas and Goals, reminders and notifications, secure input, iPhone
  tools.
- `generative-ui`: when and how to use each card tool.
- `bighelp-feed-and-ideas`: post to the agent board only when the user asks.
- `custom-theme-authoring`: make themes that the app can import.

Since 3.0.0, the agent tools have `bighelp_*` names (before, they had `loopdy_*` names). The `iphone_*` tools keep
their names. Most tools are in the `bighelp` toolset, which had the name `loopdy`. Saved tool settings and
scheduled jobs that use the name `loopdy` still get these tools. The template tools are in the `bighelp_templates`
toolset, and the workflow tool is in the `bighelp_workflows` toolset.

Card tools take
`schema: "bighelp.generative_ui"` or `"bighelp.card"`. The cards that the app gets keep their original format, so
older app builds still show them.

## Notifications (optional)

You turn on notifications in the app. Each setup is for one agent on one host. Live Activities use the same setup.
Chat never needs them.

- **Delivery.** [BuzzKit](https://buzzkit.dev) sends the notifications to your phone. The BuzzKit sending key stays
  with the bighelp notification service, never on your host. Your host gets a grant from the service. The grant
  names your phone, one agent profile and an expiry date. In the app, you choose which kinds of alerts your phone
  receives (Settings › Notifications).
- **What you get.** Replies, failed chats, scheduled tasks that finish or fail, approvals, questions from your
  agent, helper results and workflow runs. Messages between your agents alert you only if you turn them on. Each
  alert shows the name and picture of the agent. A workflow alert shows the name of the workflow. When you tap an
  alert, the app goes to that host and opens the chat.
- **End-to-end encrypted.** The app gives its own key directly to your host. The host encrypts the title, text and
  avatar of each notification for that phone, and signs it. The notification service and BuzzKit only send on
  bytes that they cannot read. They see the event type, the time, the IDs of the agent and the chat, and routing
  IDs. The format is in [`sealed_alerts.py`](loopdy_plugin/sealed_alerts.py).
- **Live Activities.** Updates on the Lock Screen and in the Dynamic Island carry only a phase, a fixed label (such
  as "Your agent is working") and counts. They never include message text.
- **Instant while bighelp is open.** A phone, iPad or Mac with bighelp open gets each alert directly from the host
  that it uses, as the same sealed alert. The device does not show an alert for the chat on its screen, and it
  tells the host that it got the alert. If the device does not answer in one second, the host sends the push as
  usual.
- **Quiet Hours.** Each phone can set a daily window in its own time zone, for example 22:00 to 07:00. In that
  window, your host sends that phone no notifications, and it does not send them later. Replies stay in the chat.
  Live Activity updates continue.

More detail: [Notifications](docs/NOTIFICATIONS.md).

## Approvals

The app uses the approvals of Hermes. It offers exactly the choices that Hermes gives (once, this session, always
or deny) and never adds a choice. You do not have to configure anything.

## Commands

```text
hermes bighelp update [--restart]         Install the latest release
hermes bighelp update-status              Show the last update result
hermes bighelp files grant|revoke|roots   Manage read-only folder grants
hermes bighelp files list|read|status|diff
```

Folder grants are for the `workspace-files` API of the plugin ([Workspace files](docs/WORKSPACE_FILES.md)). The
current app does not use them. Its Files tab shows the working folder of an agent.

`hermes loopdy …` still works for scripts written before 3.0.0.

## Legacy code

Earlier versions paired hosts through a bighelp Link account. They sent notifications through a relay or direct
APNs. The current app uses none of this. The code that remains is only for existing data and older app builds. Do
not configure it on new hosts:

- `hermes bighelp link status` and `hermes bighelp link unpair`. `unpair` removes the `LOOPDY_LINK_*` keys that
  pairing saved. `link pair` only tells you that Link no longer pairs hosts.
- `hermes bighelp status`, `provider`, `configure-apns`, `remove-apns` and `test`: the old push settings. For the
  notifications that you turn on in the app, `status` shows only how many grants are active (`notifications`).
- `hermes bighelp direct …`: the old paired transport. Its local listener starts only on a host that still has a
  Link pairing and has `direct` turned on.
- `hermes bighelp wiki …`: wiki grants for Link devices. These commands work only on a host that still has a Link
  pairing. The native wiki routes use your Hermes login.
- [Context usage](docs/CONTEXT_USAGE.md) reports go only to Link. The app reads context use from Hermes.
- The agent template routes (`native-agent-templates-v1`) stay for older app builds. The current app uses My
  Templates.

## Development

Run the tests with a separate Hermes checkout, a throwaway Hermes home and a temporary folder outside any Git
checkout. On macOS, use `/private/tmp`, not `/tmp`.

```bash
HERMES_HOME=/private/tmp/bighelp-test-home TMPDIR=/private/tmp HERMES_DISABLE_LAZY_INSTALLS=1 \
PYTHONPATH=/path/to/hermes-agent:"$PWD":"$PWD/tests" \
  /path/to/hermes-agent/venv/bin/python -m unittest discover -s tests -v

hermes plugins doctor . --ci
```

`fixtures/contracts/` has test vectors that the app shares, including the encrypted notification vector. When a
pull request changes `plugin.yaml`, `loopdy_plugin/` or `tests/`, CI runs compatibility tests against Hermes 0.21.1
and 0.21.2.

## License

Apache-2.0. The constrained JSON-tree approach of the card format credits Sameer Gupta's
[Generative UI DSL](https://github.com/sameergdogg/generative-ui).
