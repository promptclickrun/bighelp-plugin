# Security policy

## Reporting a problem

Please report security problems privately through GitHub:
**Security → Report a vulnerability** on this repository. Don't open a public issue. Include the plugin version
(`plugin.yaml`), your Hermes version and the steps to reproduce.

## Supported versions

Only the [latest release](https://github.com/promptclickrun/bighelp-plugin/releases/latest) gets fixes. Update with `hermes bighelp update`.

## How the plugin is protected

- **Hermes' own sign-in.** Every route runs inside the Hermes dashboard and requires its normal login. The
  newer `/native/*` routes also check the signed-in Hermes session, the selected agent profile and a fresh
  context tag on each request. The plugin opens no extra ports and has no account system of its own.
- **Hermes stays in charge.** Tools, approvals, chats and scheduled tasks run through Hermes. The plugin never
  widens an approval, invents a choice Hermes didn't offer, or runs a model loop of its own.
- **Nothing on disk is exposed by default.** Workspace folders and wikis are shared only when explicitly granted.
  Paths are re-checked on every read, and symlinks, credential folders and Hermes' own control folders are
  refused. Agent attachments are served by opaque ID and only if the chat really sent them.
- **Secrets stay on the host.** Provider credentials, the notification signing key and private keys are never
  returned to the app or printed by `hermes bighelp status`.
- **Notifications are sealed.** With current app builds, only the recipient phone can read an alert's text and
  picture. See [Notifications](docs/NOTIFICATIONS.md).
- **Phone tools are opt-in.** iPhone Health, Calendar and Reminders tools work only after the user enables them,
  and only while bighelp is open on that chat. See [iPhone tools](docs/IPHONE_DEVICE_TOOLS.md).

More detail: [Security and privacy](docs/SECURITY_AND_PRIVACY.md).
