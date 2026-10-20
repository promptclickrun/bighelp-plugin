# bighelp plugin installed

Restart the Hermes gateway and dashboard the way you normally do so the plugin loads.

In the bighelp app, add this host by its address on your local network or Tailscale and sign in with the host's
own login. No bighelp account, pairing code or public URL is needed. Hermes keeps owning chats, tools and
approvals.

Notifications are optional: turn them on for this host in the app. With current bighelp builds, notification content is end-to-end encrypted
for your phone.

Agents: call a visible `bighelp_render_*` tool directly to show a card in the current chat. If Hermes has
progressively disclosed it and it isn't visible, use the official tool bridge (`tool_search`, `tool_describe`,
`tool_call`) to run that exact tool. Examples: `skill_view("loopdy:generative-ui")`.

Agents learn the rest on their own: chats from the app come with a short bighelp brief, and
`skill_view("loopdy:bighelp")` covers cards, Feed, Ideas and Goals, reminders and notifications.
