# Composio Connect

In the bighelp app (Settings › Apps › Composio Apps) people sign in to their own Composio account to find and
connect apps such as mail, calendars or issue trackers. Agents use those apps through Composio Connect's MCP
server (`https://connect.composio.dev/mcp`). For that, the agent's Hermes config needs the server, turned on and
signed in to the same Composio account.

This plugin tells the app which Composio MCP servers an agent has, and can add or turn on Composio Connect.
Signing it in is Hermes' own MCP OAuth, which the app already runs.

## How it works

1. The app calls `composio/status`. If `connect` is `null`, or that server is off, it calls `composio/install`.
2. If that server's `signedIn` isn't `true`, the app starts Hermes' MCP sign-in for it
   (`POST /api/mcp/servers/<name>/auth?profile=<agent>`), opens the Composio sign-in page Hermes returns, and
   follows `GET /api/mcp/oauth/flows/<id>` until Hermes says it's done.
3. The app calls `composio/status` again to show the result.
4. When the server is ready, the app calls `composio/apps` and compares the agent's connected apps with the
   apps the person connected in the app. When the agent doesn't see them, Hermes is signed in to another
   Composio account (or one without those apps), and the app offers Hermes' MCP sign-in again.

Hermes does the work:

- Config is read and written with the helpers its dashboard MCP routes use (`_get_mcp_servers`,
  `_save_mcp_server`, `load_config` and `save_config`), inside the same profile scope and config lock
  (`config_write_scope`). A change here can't overwrite a change made at the same time on Hermes' MCP page.
- `signedIn` is Hermes' own check for saved MCP sign-in tokens (`HermesTokenStorage.has_cached_tokens`).
- When the app signs the server in, Hermes reconnects it for the agent its process serves. For other agents, and
  when install only turns a server on, the server loads in new chats, as with Hermes' own MCP page.

## Who holds the credentials

- The plugin never sees a Composio credential. The Composio account the app signs in to stays in the app, and
  the server's tokens stay in Hermes' MCP token store.
- Responses never contain tokens, header values, environment values or host paths. URLs lose their user info,
  query and fragment.
- Install writes one fixed server entry (below) or sets `enabled: true` on an existing Composio Connect entry.
  It changes nothing else in the config, never removes anything and never runs a sign-in.
- `composio/apps` connects with Hermes' own MCP code and Hermes' own sign-in for that agent, reads the tool list
  and disconnects. It calls no tool and never writes or refreshes a token itself (Hermes' code refreshes an
  expired one, as for every MCP connection). It never opens a browser sign-in on the host.

## For the app

`native-composio-v1` in `/native/context` features. It is listed when this Hermes has the MCP helpers above (Hermes
0.21.1 and later). Without it, the app says to update the plugin or Hermes.

Both routes take the usual native headers (`If-Match` context ETag and a lowercase `X-Loopdy-Request-ID`) and
the body `{"agentId": "default"}`, the Hermes profile of the agent. Install is a change, so treat a lost
response as "outcome unknown" and read the status again.

- `POST /api/plugins/loopdy/native/composio/status`: read only.
- `POST /api/plugins/loopdy/native/composio/install`: turns on or adds Composio Connect. Running it again changes
  nothing.

Both answer with the status:

```json
{
  "agentId": "default",
  "schemaVersion": 1,
  "connect": "composio",
  "servers": [
    {
      "name": "composio",
      "url": "https://connect.composio.dev/mcp",
      "enabled": true,
      "auth": "oauth",
      "kind": "connect",
      "signedIn": false
    }
  ]
}
```

- `servers`: every `mcp_servers` entry whose URL host is `composio.dev` or one of its subdomains, by name, at
  most 32.
  - `kind`: `connect` when the host is exactly `connect.composio.dev`, else `other`.
  - `auth`: `oauth` when Hermes signs in with MCP OAuth, `headers` when the entry sends its own headers (such as
    an API key), else `none`.
  - `enabled`: read the way Hermes' MCP client reads it.
  - `signedIn`: for `oauth`, whether Hermes has saved sign-in tokens for that server (they may still need a
    refresh). `null` for other kinds of sign-in, and when it can't be read.
- `connect`: the name of the first `connect` server that's on, else the first `connect` server, else `null`.

### What install changes

- A Composio Connect server exists: if none of them is on, the first one by name gets `enabled: true`. Its other
  settings stay.
- None exists: it adds this entry as `composio`, or as `composio-connect` when another server (in the config or
  from another Hermes plugin) already has that name:

  ```yaml
  mcp_servers:
    composio:
      url: https://connect.composio.dev/mcp
      auth: oauth
      enabled: true
      connect_timeout: 120
  ```

  `connect_timeout` gives Composio Connect, which lists many tools, more than Hermes' default time to connect.

### Errors

- `404 profile_not_found`: the host doesn't have that agent.
- `409 composio_name_taken`: other servers already use both names.
- `409 composio_changed`: the server entry changed while install ran. Read the status again.
- `422 invalid_request`: bad body or headers. `428`/`412`: load the context again.
- `503 composio_unavailable`: the feature isn't listed (older Hermes or plugin).
- `503 composio_install_refused`: Hermes' MCP config check refused the entry.

### The agent's apps

`native-composio-apps-v1` in `/native/context` features, listed when this Hermes also has its MCP connection code
(`_connect_server`, the MCP loop helpers and the probe-class profile scope `_config_profile_scope`). Without it, the
app shows only what `composio/status` says.

`POST /api/plugins/loopdy/native/composio/apps`, read only, with the same headers and body (`{"agentId": "default"}`):

```json
{
  "agentId": "default",
  "schemaVersion": 1,
  "connect": "composio",
  "state": "listed",
  "reason": null,
  "apps": ["gmail", "github"]
}
```

- `state`: `listed` when the agent's Composio Connect answered with its search tool. `apps` are the toolkit slugs
  its description names after "connected the apps:" (the same words the app reads for the person's own
  account). An empty list means that account has no connected apps.
- `state: "unknown"` with `apps: []` and a fixed `reason`, never a guess: `not_installed`, `turned_off`,
  `not_signed_in` (nothing connects then), `unreachable` (the connection or Hermes' sign-in failed, or it took
  longer than 30 seconds), `no_app_list` (no search tool) or `busy`.
- Bounds: one connection at a time, at most 30 seconds, at most 512 tools read and 64 KB of each description, at
  most 200 apps of at most 64 characters (`[a-z0-9_]`). A repeat within 30 seconds reuses the answer while
  Hermes' saved sign-in for that server is unchanged (a stat of its token files, no bytes read).
- Logs keep only the error type, never its text.
