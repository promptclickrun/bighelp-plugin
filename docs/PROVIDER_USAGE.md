# Provider usage

bighelp can show how much of each AI plan or balance you've used, for the tools on your Hermes computer.
The plugin finds them on its own: coding tools installed on the computer, and providers set up in Hermes.
Nothing to configure.

## What it reads

| Tool or provider | Found when | Where the numbers come from |
| --- | --- | --- |
| Claude | Claude Code is installed or signed in, or Hermes has a Claude sign-in | Claude's usage service (the same numbers as `/usage` in Claude Code): 5-hour session, week, per-model weeks, extra usage |
| Codex | The Codex CLI is installed, or Hermes has a Codex sign-in | The Codex CLI's own app server (`account/rateLimits/read`), or Hermes' `/usage` for its own sign-in: session and weekly limits, credits, banked resets |
| GitHub Copilot | The Copilot CLI or GitHub CLI is installed, or Hermes has a Copilot token | GitHub's Copilot usage (monthly credits or premium requests); the Copilot CLI's `account.getQuota` as a fallback (rounded, marked approximate). Same approach as [ghc-usage-hermes](https://github.com/promptclickrun/ghc-usage-hermes) |
| Google AI Studio | Hermes has a Gemini API key, or Gemini CLI is installed | Google has no usage API for AI Studio keys. The plugin checks that the key works and links to AI Studio's usage page |
| OpenCode | The OpenCode CLI is installed, or Hermes has an OpenCode Go or Zen key | `opencode stats` (cost, sessions and tokens for the last 7 and 30 days); OpenCode Go limits through Hermes |
| OpenRouter | Hermes has an OpenRouter key | OpenRouter's `/key` and `/credits`: balance, key limit, spend today, this week, this month |
| DeepSeek | Hermes has a DeepSeek key | DeepSeek's `/user/balance` |
| Nous Portal | Hermes is signed in to Nous Portal | Hermes' Nous Portal account: monthly credits, top-up credits |
| Other Hermes providers | A Hermes provider plugin implements `fetch_account_usage` and has a key | That plugin's own usage hook |

The provider Hermes chats with is listed first and marked.

## Read-only

- **Logins borrowed from other tools are never refreshed or rewritten.** If Claude Code's sign-in has expired,
  bighelp says to open Claude Code once. Hermes' `auth.adopt_external_logins: false` also turns off reading
  Claude Code's login here.
- CLIs run with fixed arguments, no shell, and a time limit, and refresh their own logins the way they always do.
- Keys and tokens are only sent to their own provider, and never across a redirect. They, and account emails,
  names and IDs, never reach the app.
- Results are cached for five minutes. Pull to refresh skips the cache, but not more than once every 15 seconds.
  Each provider gets 20 seconds; a slow one shows "took too long" instead of holding up the rest.

## For the app

`native-provider-usage-v1` in `/native/context` features.

`POST /api/plugins/loopdy/native/usage/list` with the usual native headers (`If-Match` context ETag and
`X-Loopdy-Request-ID`) and body `{"agentId": "default", "refresh": false}`. Hermes lookups use that profile.

```json
{
  "agentId": "default",
  "fetchedAt": "2026-09-27T20:00:00Z",
  "cached": false,
  "providers": [
    {
      "id": "claude",
      "name": "Claude",
      "status": "ok",
      "message": null,
      "plan": "Pro",
      "detectedVia": ["cli"],
      "activeInHermes": true,
      "windows": [
        {"label": "Session (5 hours)", "usedPercent": 42.0, "resetsAt": "2026-10-02T18:00:00Z", "detail": null}
      ],
      "facts": [{"label": "Balance", "value": "$25.00"}],
      "manageUrl": "https://claude.ai/settings/usage",
      "approximate": false
    }
  ]
}
```

- `status`: `ok`, `signInNeeded`, `notShared` (the provider doesn't publish usage for this kind of login), or
  `error`. Anything but `ok` has a plain `message` to show.
- `windows` are limits with a percentage used (show as bars). `facts` are label and value pairs, already formatted.
- `detectedVia`: `cli` (a tool on the computer) and/or `hermes` (set up in Hermes).
- `id` is stable: `claude`, `codex`, `codex-hermes` (only when Hermes uses a different ChatGPT account than the
  Codex CLI), `copilot`, `gemini`, `opencode`, `openrouter`, `deepseek`, `nous`, and `hermes-<provider>`.
- Times are UTC, whole seconds, ending in `Z`.
