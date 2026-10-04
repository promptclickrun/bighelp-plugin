# Template Catalog submissions

Agents can submit templates to the bighelp Template Catalog (`catalog.bighelp.app`) for the person they work
for. Nothing is published until a reviewer approves it, and the result shows up in that agent's Feed.

## Sign-in

Submitting needs a one-time GitHub sign-in on each computer. It uses GitHub's device flow with bighelp's
OAuth App, which asks for no permissions: the catalog only learns the GitHub account's ID, username and
creation date.

There are three ways in, and they share one pending sign-in:

- In chat, `bighelp_catalog_login` (`action: start`) answers with the code, the link and a QR picture. Tap
  the link on the device you're chatting on, or scan the QR with another one.
- In a terminal, `hermes bighelp catalog login` prints the same code and link with a QR drawn in text, and
  waits for approval.
- Starting again while a code is still good shows the same code.

Whichever device approves finishes the sign-in. The plugin sends GitHub's token to the catalog once and
keeps only the catalog's own install token in `plugin-data/loopdy/catalog/install.json` (owner-only). The
GitHub token is never stored.

`hermes bighelp catalog status` shows who is signed in and how recent submissions did.
`hermes bighelp catalog logout` revokes this computer's token.

## Tools

| Tool | What it does |
| --- | --- |
| `bighelp_catalog_login` | `start`, `status` or `logout`. `start` returns `userCode`, `verificationUri`, `expiresInSeconds` and, when the `qrcode` package is installed, `media` (a `MEDIA:` line for the QR picture). |
| `bighelp_submit_catalog_template` | Checks the template with the catalog's rules, then submits it. Returns `id`, `credit` (GitHub username) and `remainingToday`. |

Templates are either a `blueprint` (`board`, `category`, `text`, and `goalCategory` for goals) or an `agent`
(`name`, `role`, `vibe`, optional `description`, `category`, `instructions`).

Errors come back as `{"ok": false, "error": <code>, "message": …}`. Codes: `not_configured`,
`not_signed_in`, `invalid_template`, `network`, `github_unavailable`, `catalog_<status>` (for example
`catalog_429` at the daily limit; `field` names the problem field on a 400).

## Limits

The catalog enforces them; the plugin can't raise them.

- 5 submissions a day per GitHub account, across every computer it's signed in on.
- 10 pending at once per GitHub account.
- GitHub accounts younger than 30 days can't sign in.
- About 200 pending in total across everyone (the web form included).

## Review results

Each submission keeps its status receipt in `plugin-data/loopdy/catalog/submissions.json`. The plugin asks
the catalog how pending ones did when either tool or the status command runs, and at most every 6 hours
after a chat turn. Approvals and rejections (with the reviewer's note) are posted to Feed once. These are
plain HTTP checks and never use the AI provider.

## Settings

| Setting | Default |
| --- | --- |
| `BIGHELP_CATALOG_URL` | `https://catalog.bighelp.app` |
| `BIGHELP_CATALOG_GITHUB_CLIENT_ID` | bighelp's OAuth App |
