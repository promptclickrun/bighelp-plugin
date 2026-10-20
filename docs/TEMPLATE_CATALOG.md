# Template Catalog submissions

Agents can submit templates to the bighelp Template Catalog (`catalog.bighelp.app`) for the person that they work
for. The person approves each submission. A reviewer reads each submission before it becomes public. The agent's Feed
shows the result.

The tools are in the `bighelp_templates` toolset, with the [template tools](TEMPLATE_TOOLS.md). You can turn the
toolset off in Hermes like other toolsets.

## Sign-in

Each computer needs one GitHub sign-in before its agents can submit. The sign-in uses the GitHub device flow with
the bighelp OAuth App. The app asks for no permissions. The catalog gets only the ID, the username and the creation
date of the GitHub account.

You can start the sign-in in two places. Both use the same pending sign-in:

- In chat, `bighelp_catalog_login` (`action: start`) gives the code, the link and a QR picture. Tap the link on the
  device that shows the chat, or scan the QR with a different device.
- In a terminal, `hermes bighelp catalog login` shows the same code and link, with a QR in text. Then it waits for
  your approval.

If you start again while the code is still good, you get the same code. The sign-in ends when one device approves
it.

The plugin sends the GitHub token to the catalog one time. The catalog returns its own install token. The plugin
keeps only the install token, in `plugin-data/loopdy/catalog/install.json` (owner-only). The plugin never keeps the
GitHub token.

- `hermes bighelp catalog status` shows the GitHub user and the results of recent submissions.
- `hermes bighelp catalog logout` cancels the install token of this computer.

## Tools

| Tool | What it does |
| --- | --- |
| `bighelp_catalog_login` | `start`, `status` or `logout`. `start` gives `userCode`, `verificationUri`, `expiresInSeconds` and, if the `qrcode` package is on the computer, `media` (a `MEDIA:` line for the QR picture). |
| `bighelp_submit_catalog_template` | Examines the template with the rules of the catalog and scans it for secrets. Then it asks the person for approval and sends the template. Gives `id`, `credit` (the GitHub username) and `remainingToday`. |

## Before a template leaves the computer

The plugin does these steps in this sequence. If a step fails, the plugin sends nothing and stops:

1. It examines the template with the rules of the catalog (see [Templates](#templates)).
2. It scans all the text of the template for keys and passwords. This is the same scan as
   [My Templates](MY_TEMPLATES.md).
3. It makes sure that the computer has a catalog sign-in.
4. It asks the person in the approval prompt of Hermes (in the terminal, a chat platform or the bighelp app). The
   prompt shows the GitHub username, the template and its text. Hermes asks again for each submission.
5. It sends the template only with the sign-in that the person approved.

The tool does not ask, and sends nothing, if no person can answer the prompt. This occurs in these conditions:

- Approvals are off (`--yolo` or `approvals.mode: off`).
- The run is a scheduled job, a single-query run (`-q`) or a run on an unattended platform.
- The run is a workflow stage.

## Templates

The plugin uses the same rules as the catalog (`services/catalog` in promptclickrun/bighelp). Thus the catalog
refuses a template only for its limits, never for a mistake that the plugin can find.

A `blueprint` is a prompt for the Feed, Ideas or Goals board:

- `board`: `feed`, `ideas` or `goals`.
- `category`: `productivity`, `marketing`, `content`, `personal` or `research`.
- `text`: 20 to 1,000 characters.
- `goalCategory` (goals only): `health`, `relationships`, `finance`, `career`, `interests`, `productivity` or
  `other`. The default is `other`.

An `agent` is an agent personality:

- `name` (1 to 40 characters), `role` (2 to 80), `vibe` (2 to 120) and an optional `description` (up to 240).
- `category`, one of ten topics: `productivity`, `business`, `coding`, `science`, `education`, `health`,
  `creative`, `personal`, `leadership` or `security`.
- `instructions`: 80 to 12,000 characters.
- `variables` (optional): the fields that the app asks for before it completes the text. Up to 12.

The text of an agent template can use placeholders:

- `{{agent_name}}` is the name that the person gives the agent. `{{user_name}}` is the name of the person.
- Each other `{{key}}` in `instructions`, `role` or `description` needs a declared variable. Each declared
  variable must occur in the text.
- `name` and `vibe` cannot have placeholders.

The rules for variables are in
[TEMPLATE_VARIABLES.md](https://github.com/promptclickrun/bighelp/blob/main/services/catalog/docs/TEMPLATE_VARIABLES.md)
in promptclickrun/bighelp.

The plugin counts characters the same way as the catalog: an emoji counts as two. The catalog refuses a request
that is larger than 32 KB, so the plugin refuses it first.

## Errors

An error has the form `{"ok": false, "error": <code>, "message": …}`. The codes are:

| Code | Meaning |
| --- | --- |
| `invalid_template` | The template does not obey a rule of the catalog. `field` gives the field to correct, for example `variables[2].options`. |
| `secret_scan_blocked` | The template has text that looks like a key or a password. |
| `too_large` | The template is larger than 32 KB. |
| `not_signed_in` | The computer has no catalog sign-in, or the sign-in changed before the person answered. |
| `approval_unavailable` | No person can answer the approval prompt now. |
| `not_approved` | The person did not approve the submission, or the prompt failed. |
| `not_configured` | This plugin version cannot start a GitHub sign-in. |
| `network`, `github_unavailable`, `catalog_unexpected`, `response_too_large` | GitHub or the catalog did not answer correctly. |
| `catalog_<status>` | The catalog refused the request, for example `catalog_429` at the daily limit. |

## Limits

The catalog sets these limits. The plugin cannot change them.

- 5 submissions each day for each GitHub account, on all the computers of that account.
- 10 pending submissions at one time for each GitHub account.
- A GitHub account that is younger than 30 days cannot sign in.
- Approximately 200 pending submissions in total from all people (the web form too).

## Review results

The plugin keeps a status receipt for each submission in `plugin-data/loopdy/catalog/submissions.json`. It asks
the catalog about pending submissions at these times:

- When a tool or the status command runs.
- After a chat turn, but not more than one time in 6 hours.

The plugin puts each approval or rejection in the Feed one time, with the note of the reviewer. These checks are
plain HTTP requests. They never use the AI provider.

## Settings

| Setting | Default |
| --- | --- |
| `BIGHELP_CATALOG_URL` | `https://catalog.bighelp.app` |
| `BIGHELP_CATALOG_GITHUB_CLIENT_ID` | The bighelp OAuth App |

`BIGHELP_CATALOG_URL` is for tests on this computer. The plugin sends the tokens to this address, so it accepts
only these values:

- `https://catalog.bighelp.app`
- An address on this computer, with a port: `http://127.0.0.1:<port>` or `http://[::1]:<port>`

The plugin compares the scheme, the full host name and the port. For all other values, for example
`http://127.0.0.1.example.com`, the plugin uses the real catalog.
