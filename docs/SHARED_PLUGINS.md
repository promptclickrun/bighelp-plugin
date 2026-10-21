# Shared plugins

When Hermes clones an agent, it copies the `plugins/` folder of the source agent (`_clone_plugins`). The new
agent turns the same plugins on. bighelp makes agents that way: **New agent**
and the **Create** button of Simple Bot mode clone the default agent.

Hermes puts the plugins of all agents into one shared Python environment, with one member for each real
plugin folder. Some plugins ship their own buildable package (a `pyproject.toml` with `[build-system]`). After a clone,
such a package is there two times with one name. uv then refuses every later plugin install on the host,
including the updates of the bighelp plugin:

```
Error: Plugin 'loopdy' was not published: venv: uv lock exited 2: error: Two workspace members are both named `weather-mem`
```

A link resolves to the folder of the default agent, so Hermes sees one package. An agent cloned from an agent
that has a link also gets a link. The agents that share a plugin also get its updates.

## When a copy becomes a link

The plugin replaces a plugin folder of an agent with a link to the folder of the default agent only if:

- the agent's folder is a real folder, not a link, and the default agent has a folder with the same name
- the install records (`plugins/.install-metadata.json`) of the two agents give the same `source` and
  `revision`
- the files are the same (`.git`, `__pycache__`, `.pyc` files and `.DS_Store` are not compared)

The plugin does not change anything else: the plugins of the agent itself, other versions and local changes.
If a plugin writes files into its own folder, the agents that share it also share those files. A copy whose
files changed after the clone does not match, so it stays a copy.
The swap holds the installer's lock (`.install-metadata.json.lock`). If a step fails, the plugin restores the copy.
The plugin does not compare trees with more than 20,000 files or 512 MB. On Windows, nothing changes.

## When it runs

- The app calls `POST /api/plugins/loopdy/native/agents/plugins/share` right after it makes an agent.
- Each native context request starts a pass over all agents in the background, if the set of agents changed
  since the last pass of that process. The first pass repairs the agents that a host already has. Later
  passes repair agents that Hermes Desktop or the CLI makes.

## Route contract

Capability: `native-agent-plugin-links-v1`. The context lists it only on a POSIX host where Hermes' profile
helpers import.

`POST /api/plugins/loopdy/native/agents/plugins/share`, with `If-Match` (the context ETag) and
`X-Loopdy-Request-ID`. Body:

```json
{"agentId": "ralph"}
```

Response (`no-store`), at most 100 names in each list:

```json
{"linked": ["weather"], "kept": [{"name": "rss", "reason": "different_install"}]}
```

`reason` is one of `not_installed` (no install record to compare), `different_install`, `changed`,
`too_large` or `error`. The default agent answers with two empty lists.

| Status | Code | When |
|---|---|---|
| 404 | `profile_not_found` | The agent does not exist. |
| 409 | `plugins_busy` | A plugin install holds the lock of that agent for more than 10 seconds. |
| 412 | `context_changed` | The context changed. Refresh it and try again. |
| 422 | `invalid_request` | The body is not exactly `{"agentId": …}`. |
| 503 | `plugin_links_unavailable` | The context does not list the capability. |

The response has plugin names only, never paths.
