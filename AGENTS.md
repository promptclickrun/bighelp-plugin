# AGENTS.md: working on the bighelp plugin

This guide is for AI agents and people picking up work on the Hermes plugin behind the
[bighelp](https://github.com/promptclickrun/bighelp) iPhone app. It covers how we work, how the plugin fits into
Hermes, and the traps that have already cost us time. Read it before you change anything.

## What this plugin is

It runs inside a user's own Hermes host and adds what the app needs on top of stock Hermes: agent boards
(Feed/Ideas/Goals), native cards, iPhone tools, live voice, files and wikis, provider usage, secure input, templates
and optional end-to-end encrypted notifications. The README lists every feature and links its doc in `docs/`.

Hermes stays in charge of agents, chats, tools, approvals and scheduled tasks. The plugin adds to Hermes through its
official extension points: hooks, tool registration, the dashboard plugin API and server requests. It never
replaces or patches Hermes internals.

Many features span both repos. The app's side of each route lives in the app repo's
`Bighelp/DirectHermes/DirectHermesNativeContext.swift`.

## Mindset

- **Releases are batched, and the maintainer starts them.**
  - Pull requests don't change the version. Merged changes collect on `main`, which must stay releasable.
  - When the maintainer decides to ship, a release PR bumps `plugin.yaml` and `PLUGIN_VERSION` and lists
    everything since the last release. Its title leads with the version. Its description becomes the release
    notes, and the app shows the first 1,500 characters on its update screen, so open with a short summary in
    plain words for people who run hosts. Credit outside contributors.
  - After it merges, the maintainer runs the Release workflow (`gh workflow run release.yml`). It publishes the
    version from the release PR's merge; anything merged later waits for the next release.
  - The bighelp app offers the latest release to every host, and `hermes bighelp update` installs it. No app
    build is needed to ship a plugin update.
  - The maintainer merges.
- **Never break older apps.** Add fields and add new capability names. Don't rename, remove or change the meaning of
  what an app build already uses.
- **Private by default:**
  - Responses to the app never contain keys, tokens, emails, account IDs or host file paths.
  - Log fixed error codes and stages, never exception text, because exception text can carry secrets or paths.
- **Borrow, don't touch.**
  - When reading another tool's sign-in (Claude Code, Codex CLI, Copilot), read it and never refresh or rewrite it.
    Their refresh tokens rotate, so a refresh here signs that tool out.
  - Use Hermes' read-only credential helpers.
- **No AI spending by default.**
  - Nothing runs on the user's AI provider by itself: no default scheduled jobs, posts or model calls.
  - Bundled skills teach agents to act only after the user asks.
- **Bounded and validated.** Every input, response and outside call has a size cap, a timeout, and caching or
  throttling where hosts or providers could be hammered.
- **Errors that explain themselves.**
  - The native route wrapper turns unexpected exceptions into a generic 503 `native_service_unavailable`. That once
    hid a live voice failure completely.
  - For failures you expect, raise `NativeAPIError` with a specific code (for example `voice_provider_<reason>`), and
    log the code and stage.
- **Reproduce first, then fix the root cause.** Write a failing test before the fix. Make fakes behave like real
  Hermes.
- **Plain language.** Docs, user-facing messages and commit subjects are short and clear. Match the surrounding code
  and comment density.
- **This repo is public.** Use made-up numbers and names in docs, tests, fixtures and issues. Never use a real
  person's plans, balances, hosts or accounts.

## Where things live

| Path | What it is |
|---|---|
| `__init__.py`, `loopdy_plugin/registration.py` | Entry point: registers the platform adapter, hooks, tools and skills with Hermes |
| `loopdy_plugin/native_api.py` | The app's routes under `/api/plugins/loopdy/native/…` (`router`, `_NativeRoute`, `NativeAPIError`) |
| `loopdy_plugin/native_context.py` | `/native/context`: the feature list the app checks before calling a route |
| `loopdy_plugin/tools.py`, `generative_ui.py`, `loopdy_cards.py` | Agent tools and native cards (`bighelp_render_*`) |
| `loopdy_plugin/agent_guide.py`, `skills/bighelp/` | What agents are told about bighelp: the chat brief and the `bighelp` skill |
| `loopdy_plugin/agent_board.py`, `provider_usage.py`, `secure_input.py`, `agent_templates.py`, `reactions.py` | Feature modules |
| `loopdy_plugin/live_voice_*.py`, `native_voice.py`, `voice_*.py` | Live and turn-based voice |
| `loopdy_plugin/managed_notifications*.py`, `sealed_alerts.py` | Notifications and Live Activities |
| `loopdy_plugin/plugin_update*.py`, `host_restart.py` | Self-update and the in-place restart |
| `loopdy_plugin/link_*`, `relay_*`, `direct_*`, `provider.py` | Legacy pairing and relay code, kept only for compatibility |
| `dashboard/` | Dashboard plugin manifest and `plugin_api.py` routes |
| `skills/` | Read-only guidance for agents (`skill_view("loopdy:<name>")`) |
| `spec/`, `fixtures/contracts/` | Card schemas and test vectors shared with the app |
| `docs/` | One page per feature, with its route contract |
| `tests/` | `unittest` suites |

## Names

Everything an agent or a person reads says bighelp: tool names and the `bighelp` toolset, skills, prompts, labels,
messages, logs, the `hermes bighelp` command and `BIGHELP_*` settings. `loopdy_plugin/naming.py` holds these names
and the old ones that keep working.

The plugin was first called Loopdy. These names are stored on hosts or read by app builds already on phones, so
they stay until every supported app build speaks the new ones:
- the plugin name in `plugin.yaml` (so the `plugins/loopdy` folder, `plugins.enabled: [loopdy]` and the
  `loopdy:` skill prefix, which follows it)
- the `/api/plugins/loopdy/…` routes, the `X-Loopdy-Request-ID` header and the `plugin-data/loopdy` folder
- card wire formats (`loopdy.generative_ui`, `loopdy.card`, the `loopdy-card` fence): agents write
  `bighelp.generative_ui` and `bighelp.card`, and the plugin translates
- the `loopdy` platform target and the notification formats shared with the app and the notification service
- the `loopdy_plugin` package name (internal)

Old names that still work: `hermes loopdy …` (hidden), the `loopdy` toolset (an alias for `bighelp`), and
`LOOPDY_*` settings when the `BIGHELP_*` one isn't set.

## How the plugin loads (read this before touching shared state)

One Hermes dashboard process holds **two separate copies** of the plugin's modules:
- the dashboard API loader adds the plugin root to `sys.path` and imports top-level `loopdy_plugin`
- Hermes' plugin loader imports `hermes_plugins.loopdy.loopdy_plugin` for hooks and tools

Module-level singletons are therefore duplicated. State a hook records is invisible to an HTTP route unless it's
shared through one `sys.modules` entry (see `sys.modules.setdefault(...)` in `managed_notifications.py`). When a route
"can't see" something a hook did, suspect this first. This bug once kept every phone's Live Activity stuck on
"Updates paused".

## Adding a feature for the app

1. **Route:** add it to `native_api.py`. Raise `NativeAPIError(status, code, message)` for expected failures.
   - Requests carry `If-Match`, the context ETag.
   - `X-Loopdy-Request-ID` must be a lowercase UUID, and responses echo it.
   - Responses are `no-store`.
2. **Capability:** add a constant like `native-<name>-v1`. In `native_context.py`, list it only when the feature can
   actually work, and otherwise `skip(...)` it with a reason.
3. **Tests:**
   - Add the feature to the expected list in `tests/test_native_api.py`.
   - Add it to `PROCESS_FEATURES` in `tests/test_native_startup_logging.py`.
   - Add unit tests for the module itself.
4. **Docs:** add `docs/<FEATURE>.md` with the full route contract, and a row in the README feature table.
5. **Version:** leave it alone. The next release PR bumps `plugin.yaml` and `PLUGIN_VERSION` in
   `loopdy_plugin/link_contracts.py` together; `tests/test_portability.py` checks that they match.
6. **After the release:** the app offers the new version to hosts. The app adds its side of the route in its own
   release.

## What agents are told

- The app starts and reopens chats with session source `bighelp`. Hermes has no built-in hint for it, so the
  plugin's prompt section (`agent_guide.py`) is the agent's description of where it is. Before, chats had no
  source and Hermes called them its terminal UI: no files, no cards, "cron can't reach you".
- The section is rendered once per new session and frozen, and Hermes caps all plugins' sections together at
  8,000 characters. Keep the brief short and put detail in `skills/bighelp/SKILL.md`.
- Plugin skills are not listed in the agent's skill index. Agents find them only through the brief or another
  skill, so link new skills from `skills/bighelp/SKILL.md` and register them in `agent_guide.SKILLS`.
- A scheduled job's reply reaches the phone as a managed notification from the run itself. Guidance says
  `deliver: "local"`: an omitted `deliver` falls back to another channel's home chat, and `loopdy` delivery is
  the retired Link inbox.

## Hermes compatibility

- The plugin supports Hermes 0.21.1 and later. CI runs the compatibility suite against 0.21.1 and 0.21.2, and the app
  supports hosts on 0.21.2 to 0.21.5.
- `provides_hooks` in `plugin.yaml` lists only baseline hooks, because older Hermes validates those names. Register
  newer hooks, such as `on_room_member_activity`, only when the running Hermes advertises them.
- Hermes may hide tools behind progressive disclosure (`tool_search`, `tool_describe`, `tool_call`). Skills tell
  agents to use that bridge to run the exact tool, never a substitute.
- Server requests (clarify, approval, secret) reach the app only if the app has advertised that it answers them.
  Secure input rides Hermes' own secret-capture callback: the value goes to the agent's env file and never to the
  model.

## Updates must stay unattended

- The self-updater pins the latest release to its commit, validates it, backs up the current copy and installs it
  with Hermes' installer. It refuses to overwrite a modified install or move a host to an older version.
- **New `capabilities:` in `plugin.yaml` block automatic updates** until someone approves them on the host. Avoid
  this unless it's truly needed, and call it out in the PR.
- **Hermes scans the whole plugin on every update**, docs included. A `caution` verdict needs a person to approve it,
  so the update stalls on unattended hosts.
  - The scan looks for things like shell pipes into interpreters, reads of secret files, instructions to edit agent
    config, and token-shaped examples.
  - The limits are 400 files and 10 MB.
- Before merging, scan a clean export of your branch with a separate Hermes checkout, and make sure the verdict is
  still `safe` with no high or critical findings:

  ```python
  from pathlib import Path
  from tools.plugin_guard import scan_plugin, should_allow_plugin_install
  result = scan_plugin(Path("/path/to/clean/export"), source="https://github.com/promptclickrun/bighelp-plugin")
  print(result.verdict, should_allow_plugin_install(result, force=False))
  ```

## Testing

```bash
HERMES_HOME=/private/tmp/bighelp-test-home TMPDIR=/private/tmp \
PYTHONPATH=/path/to/hermes-agent:"$PWD":"$PWD/tests" \
  /path/to/hermes-agent/venv/bin/python -m unittest discover -s tests -v

hermes plugins doctor . --ci
```

- Set `HERMES_HOME` before anything imports Hermes. On macOS use real, non-symlinked temp folders (`/private/tmp`,
  not `/tmp`) that aren't inside a Git checkout.
- Use a separate clone of Hermes for tests, never the checkout a real install runs from.
  - **Never start a Hermes dashboard with a fresh `HERMES_HOME` against a shared checkout.** Hermes treats it as an
    unfinished update, rebuilds the checkout and rewrites the real install's launchers.
  - Set `HERMES_DISABLE_LAZY_INSTALLS=1`.
- Never call `/api/gateway/restart` from a test host. On macOS it reaps every gateway process on the machine.
- Some tests are flaky or environment-bound on `main` too (a `test_native_device_tools` middleware test can hang for
  about 30 seconds). Compare with `main` before calling something a regression.
- **End-to-end with the app:**
  - The app repo's `Scripts/HostSignInMatrixProbe.py --plugin <this checkout>` starts isolated hosts, with scripted
    tool turns under `--modes tools`.
  - Test hosts need `plugins.enabled: [loopdy]`, or user plugins don't load.
- **After updating a running host,** restart every dashboard process the app talks to, not just the gateway.
  - Dashboards keep the old code in memory.
  - When the version string didn't change, a stale process looks identical from outside.

## Notifications

- Alerts are sealed end to end (v2). The host encrypts each alert's title, text and avatar for one phone and signs it,
  so the notification service and BuzzKit see only placeholders and routing data.
- `fixtures/contracts/sealed-alert-v2-vector.json` must match the app's copy byte for byte.
- The envelope limit is 2,300 bytes, so an alert fits in one push.
- Live Activity updates carry only a phase, a fixed label and counts, never message text.
- Changes to the notification contract roll out in order: the relay service, then the app, then the plugin. Coordinate
  with the maintainer.
- The legacy Link, relay, direct APNs and paired-transport code stays only for compatibility. Don't extend it or
  document it for new hosts.

## Git and pull requests

- Work on a branch and open a PR. `main` takes squash merges only, and the maintainer merges.
- **Subjects:**
  - Say what changed in plain words.
  - Releases lead with the version, for example `2.18.0: provider usage for the bighelp app`.
- **PR descriptions:** explain what hosts and the app will see, the privacy and trust impact, what you tested, and
  against which Hermes versions.
- Commit your work before you stop, and clean up temp homes, clones and processes you started.
