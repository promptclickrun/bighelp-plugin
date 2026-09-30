# Provider sign-in

Some AI accounts only sign in from a terminal on the Hermes computer. With this feature, bighelp's Provider Keys
screen signs in to them from the phone: the plugin runs the provider's own sign-in tool on the computer, and the
phone shows its link and code. Accounts Hermes already signs in to itself (Nous Portal, ChatGPT/Codex, MiniMax,
xAI Grok) keep using Hermes' own sign-in.

## What it signs in to

| Account in Hermes | Tool that signs in | How it goes on the phone |
| --- | --- | --- |
| GitHub Copilot (ACP) (`copilot-acp`) | GitHub Copilot CLI: `copilot login --device-code` | Code and link; GitHub confirms on its own |
| GitHub Copilot (`copilot`) | Hermes' own GitHub login, the same one `hermes model` runs. The token goes to the profile's env as `COPILOT_GITHUB_TOKEN` | Code and link; GitHub confirms on its own |
| Claude Code (`claude-code`) | Claude Code: `claude auth login --claudeai` | Link; paste the code Claude shows back into bighelp |
| Claude Subscription DirectSDK (`claude-subscription-directsdk-experimental`) | Claude Code, with the plugin's `CLAUDE_SUBSCRIPTION_DIRECTSDK_COMMAND` and `CLAUDE_SUBSCRIPTION_DIRECTSDK_CONFIG_DIR` | Link; paste the code back |
| Anthropic Account (`anthropic`) | Claude Code: `claude setup-token`. Its long-lived token goes to the profile's env as `CLAUDE_CODE_OAUTH_TOKEN`, the route Hermes documents for Claude subscriptions | Link; paste the code back |
| Qwen OAuth (`qwen-oauth`) | None. Qwen stopped offering this sign-in in April 2026 | Listed as retired, with Qwen Cloud's API key as the replacement |

A sign-in is listed only when this Hermes has that provider. That includes GitHub Copilot, which Hermes files under
API keys because it keeps a token, but which signs in with GitHub like an account.

If the tool isn't installed, the app says so and shows the install command. Nothing is installed for you.

## Respecting provider terms

- The provider's own client does every sign-in. bighelp never contacts a sign-in service, never builds its own
  OAuth request, and never reads or copies another tool's saved login.
- Hermes keeps its own Claude sign-in (`hermes auth add anthropic`) in the terminal on purpose, so bighelp never
  runs it. Claude accounts sign in through Claude Code, Anthropic's own client.
- A sign-in a provider ends is marked retired instead of started.

## Safety and privacy

- Commands are fixed per provider in `loopdy_plugin/provider_sign_in.py`. The phone only names a provider; it can't
  pass arguments, and nothing runs through a shell.
- The tool runs in a private terminal on the computer. Its attempts to open a browser there do nothing.
- Only three things reach the phone: an `https` link on the provider's own domains, the one-time code to enter
  there, and whether it worked. Terminal output stays in memory for that one sign-in and is never logged.
- A token a tool prints for you to keep is saved straight to the profile's env with Hermes' credential helper,
  then dropped. It never reaches the phone or the logs.
- A pasted code must be one line of visible text, up to 2,048 characters.
- A sign-in stops after 15 minutes. At most four run at once, and starting one again replaces the unfinished one.
- Linux and macOS only (it needs a POSIX terminal).

## Adding a provider

Add a `Recipe` to `RECIPES` in `loopdy_plugin/provider_sign_in.py`:

- `provider_id`: the account's id in Hermes' `/api/providers/oauth` list. The app matches on it.
- `command`: the provider's own CLI and fixed arguments, usually `_cli("tool", override=(...settings))`.
- `flow`: `DEVICE` when the provider's page confirms by itself, `PASTE` when it shows a code to paste back.
- `link_hosts`: the provider's own domains. Links anywhere else are never shown.
- `code`: for device flows, the pattern of the code the tool prints.
- `rejected`: what the tool prints when a pasted code doesn't work, if it doesn't exit.
- `signed_in`: an optional local check (no network) that the account is signed in.
- `retired`: a message, when the provider has ended the sign-in.

Only add a provider when its own client does the sign-in, and add tests with a fake of its CLI that prints what
the real one prints. The app needs no update for a new provider.

## For the app

`native-provider-sign-in-v1` in `/native/context` features.

`POST /api/plugins/loopdy/native/provider-sign-in/<operation>` with the usual native headers (`If-Match` context
ETag and `X-Loopdy-Request-ID`). Every body has `agentId`, the profile.

| Operation | Body | Answer |
| --- | --- | --- |
| `list` | `{"agentId"}` | `{"agentId", "providers": [provider]}` |
| `start` | `{"agentId", "providerId"}` | a session, after waiting up to 12 seconds for the link |
| `status` | `{"agentId", "sessionId"}` | a session |
| `submit` | `{"agentId", "sessionId", "code"}` | a session (`finishing`) |
| `cancel` | `{"agentId", "sessionId"}` | a session (`cancelled`) |

A provider:

```json
{
  "providerId": "claude-code",
  "name": "Claude Code",
  "client": "Claude Code",
  "flow": "paste",
  "state": "ready",
  "signedIn": false,
  "docsURL": "https://docs.claude.com/en/docs/claude-code/setup"
}
```

`state` is `ready`, `notInstalled` (with `message` and `installCommand`) or `retired` (with `message` and, when
there is one, `replacementKey`, the Hermes env name of the key to use instead). `signedIn` is present only when the
tool can say.

A session:

```json
{
  "sessionId": "5f0c3c52-3a8e-4b8f-9d37-0d3c0e6f1a2b",
  "providerId": "copilot-acp",
  "flow": "device",
  "status": "waiting",
  "link": "https://github.com/login/device",
  "code": "WDJB-MJHT",
  "expiresInSeconds": 890
}
```

`status` is `starting`, `waiting` (device: open the link, enter the code), `needsCode` (paste: open the link, then
send the page's code), `finishing`, `signedIn`, `failed`, `expired` or `cancelled`. `link` and `code` are dropped
once it ends. `message` says what went wrong in plain words.

Errors use the usual native error body. Codes: `sign_in_unavailable` (404, no sign-in for that provider),
`sign_in_not_found` (404), `sign_in_tool_missing` (409), `sign_in_not_waiting` (409), `sign_in_retired` (410),
`sign_in_code_invalid` (422), `sign_in_busy` (429), `sign_in_start_failed` (503) and `sign_in_host_unavailable` (503).
