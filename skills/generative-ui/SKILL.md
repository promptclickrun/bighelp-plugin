---
name: generative-ui
description: Render bounded v1 or v2 native bighelp cards when structured presentation is clearer than prose.
---

# bighelp Generative UI

Use a bighelp renderer when the user asks for a card, dashboard-like result, metrics, a bounded list, or a sequence of events. Keep normal prose when structured presentation adds no value.

## bighelp Cards

Use the generic `bighelp_render_card` tool, documented in `references/bighelp-cards.md`, for new static compositions that do not match a typed renderer. These are called **bighelp Cards**. Use typed v2 for existing polished use cases until generic rendering reaches visual parity. In this release, `data_sources` must be empty and all displayed values must be embedded in the card payload; live device refresh is not available. Cards allow no downloaded code, HTML, WebViews, authenticated requests, or secrets.

The renderers are first-class model tools in the `bighelp` toolset. If the exact
renderer is visible in the current tool list, call it directly. If Hermes has
progressively disclosed plugin tools and that renderer is absent, use the
official `tool_search`, `tool_describe`, and `tool_call` bridge to find and
invoke that exact renderer. Do not route a visible renderer through the bridge,
wrap it in another tool, or invent a replacement.

## Choose the delivery path first

### A. Inline card in the active chat

Use this path when the card answers the message in the conversation currently
open in bighelp. Call the renderer, then put its returned `display_markdown` in
your reply exactly once.
Do not use the notification channel just to answer the current chat, and don't
schedule a job to show a card now.

Example instruction for an active weather chat:

> Fetch the forecast and call `bighelp_render_weather_forecast` for this response.
> Put its `display_markdown` in the answer once.

### B. Proactive or scheduled card

A scheduled job's run is its own chat. When it finishes, bighelp notifies the
user with the start of the run's reply, and tapping the alert opens the run
(see `skill_view("loopdy:bighelp")`). So a scheduled card works like an inline
one: the run calls the renderer and puts the returned `display_markdown` in its
final reply once, after one plain sentence that reads well as a notification.
Create the job with `deliver: "local"`; bighelp's alert comes from the run
itself.

Example task instruction:

> Fetch tomorrow's forecast for Lisbon. Reply with one sentence on what to
> expect, then call `bighelp_render_weather_forecast` and add its
> `display_markdown` once.

A renderer result does not auto-forward anywhere: only the run's final reply is
kept and notified. For something the user should find later rather than be
alerted about, post it to the Feed with `bighelp_board` instead. Don't deliver
cards to `loopdy`: that's the retired Link inbox, which the app no longer shows.

Never script or reconstruct a renderer envelope. Call the official renderer
and use its exact returned `display_markdown`. Do not rebuild JSON from the
visual result or assume an earlier tool call will be attached later.

For current weather or forecast requests, fetch the current data and then call
`bighelp_render_weather_forecast` directly with the strict v2 payload below when
a card would make the result easier to use. Do not stop at a prose-only forecast
when the native weather card is relevant.

Choose one renderer. The original v1 tools remain compatible:

- `bighelp_render_summary` for a title and short body.
- `bighelp_render_metrics` for up to 20 labeled scalar values.
- `bighelp_render_list` for up to 20 short items.
- `bighelp_render_timeline` for up to 20 ordered steps.

Use v2 for typed current-data and interactive cards:

- `bighelp_render_weather_forecast`
- `bighelp_render_sports_game`
- `bighelp_render_stock_quote`
- `bighelp_render_chart`
- `bighelp_render_dashboard`
- `bighelp_render_form`
- `bighelp_render_checklist`
- `bighelp_render_selection`: each chosen option's `stage_text` is put in the
  user's message box; nothing is sent until they send it.
- `bighelp_render_automation`: one real scheduled job with Pause, Resume or Run
  buttons that act on it directly. Use the job's actual ID, profile and state.

V2 calls use `schema: "bighelp.generative_ui"`, `version: 2`, the matching
component, and the exact strict tool schema. Current-data cards require source
timestamps and freshness provenance. The renderer derives `age_seconds` from
those timestamp facts, so it may be omitted (or supplied as stale model
metadata). Forms are bound by the host to the exact
profile and session. After rendering a form, call
`bighelp_await_form_response` with only its server-generated `request_id`; do
not invent an endpoint, route, command, URL, or action target.

Every v1 call uses `version: 1`, the matching `component`, and an optional short `title`. Do not add URLs, HTML, styles, routes, actions, or executable content.

## Examples

Summary:

```json
{"version":1,"component":"summary","title":"Build status","body":"All verification checks passed."}
```

Metrics:

```json
{"version":1,"component":"metrics","title":"Task checks","metrics":{"Passed":18,"Failed":0,"Duration":"4m 12s"}}
```

List:

```json
{"version":1,"component":"list","title":"Next steps","items":["Review the diff","Run the device smoke test","Prepare release notes"]}
```

Timeline:

```json
{"version":1,"component":"timeline","title":"Deployment","steps":["Build completed","Checks passed","Ready for approval"]}
```
