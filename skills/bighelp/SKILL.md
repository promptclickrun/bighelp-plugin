---
name: bighelp
description: Start here for anything in the bighelp app. Where you are, cards, forms, Feed, Ideas and Goals, reminders and notifications, secure input and iPhone tools.
---

# bighelp

bighelp is the user's iPhone, iPad and Vision Pro app for this Hermes host. It
talks straight to Hermes, so chats, tools, approvals and scheduled jobs are
Hermes' own. Chats started in the app have session source `bighelp`. It's a
chat app, not a terminal and not Hermes Desktop.

All the bighelp tools below are in the `bighelp` toolset. If a tool isn't in
your current tool list, Hermes has hidden it behind progressive disclosure:
find and run that exact tool with `tool_search`, `tool_describe` and
`tool_call`. Never swap in a different tool.

## What the chat shows

- Markdown: headings, lists, quotes, code blocks, tables and links. Web images
  in Markdown don't display.
- Files, pictures and videos: put `MEDIA:/absolute/path` on its own line. The
  app shows it as a picture, video or file card. Hermes' media settings decide
  which folders can be sent; if a file doesn't show, tell the user where it is.
- Keep replies short and conversational. People read them on a phone.

## Asking the user

- Hermes' `clarify` questions and approval requests appear as native prompts.
  Ask one clear question at a time.
- For a password, API key or other secret, call
  `bighelp_request_secure_input`. The value goes straight to the agent's
  environment and never into the chat or to the model. Never ask for a secret
  in a normal message.

## Cards

Use a card when it's clearer than prose. Call exactly one renderer, then put
the `display_markdown` it returns in your reply exactly once. The tool call on
its own shows nothing.

| Need | Tool |
|---|---|
| Weather or a forecast | `bighelp_render_weather_forecast` |
| A game score | `bighelp_render_sports_game` |
| A stock quote | `bighelp_render_stock_quote` |
| A chart | `bighelp_render_chart` |
| A small dashboard | `bighelp_render_dashboard` |
| A form the user fills in | `bighelp_render_form`, then `bighelp_await_form_response` with its `request_id` |
| Options to pick from | `bighelp_render_selection`: each chosen option's `stage_text` goes into the user's message box, and they send it |
| A checklist | `bighelp_render_checklist` |
| A scheduled job, with Pause, Resume and Run buttons | `bighelp_render_automation`, using the job's real ID, profile and state |
| A short summary, metrics, a list, a timeline | `bighelp_render_summary`, `_metrics`, `_list`, `_timeline` |
| Any other static layout | `bighelp_render_card` |
| A saved card template | `bighelp_search_card_templates`, `bighelp_get_card_template`, `bighelp_render_card_template` |

Cards hold only the values you put in them: no live data, code, HTML or
secrets. Current-data cards need their source and time. Payload rules and
examples: `skill_view("loopdy:generative-ui")`.

## Feed, Ideas and Goals

The app has three boards beside the chat. Write to them with `bighelp_board`:
`post` for the Feed, `idea` for things you offer to do, `goal` and
`update_goal` for goals and things you keep track of, `list` to see what's
there (with the user's thumbs up or down), `remove` to delete one. Publish only
what the user asked for. Details: `skill_view("loopdy:bighelp-feed-and-ideas")`.

## Reminders, check-ins and scheduled updates

bighelp has no separate channel to send to. When a scheduled job for this
agent finishes, bighelp sends the user a notification with the run's reply, if
they turned on notifications for this agent (Settings, Notifications). So:

1. Use Hermes' `cronjob` tool with `action: create` for this agent.
   - One reminder: a one-shot schedule, `in 2h` for a delay or an ISO time
     such as `2026-06-01T09:00:00` for a set moment.
   - Something recurring: a recurring schedule, only when the user asked for it.
2. Set `deliver: "local"`. If you leave it out, Hermes also posts the reply to
   another connected channel, such as a messaging app's home chat. Don't use
   `deliver: "loopdy"`: that's the retired Link inbox and fails on current setups.
3. Write the job's prompt so its final reply is the message itself. The
   notification shows the start of that reply, so put the important words
   first ("Time to call the dentist, they close at 5.").
   - To skip a run with nothing worth saying, reply `[SILENT]`.
   - For a card, start with one plain sentence, then the card's `display_markdown`.
4. If the job needs bighelp tools (for example `bighelp_board` for a Feed
   post), and you give it its own `enabled_toolsets`, include `bighelp`.
5. Show the job with `bighelp_render_automation` if it helps: the user can pause,
   resume or run it from the chat.
6. Name the job so the user recognises it. Then tell them in one sentence what
   will run and when, and that they can change or stop it in bighelp's
   Scheduled tasks. If you don't know whether their notifications are on, say
   the alert needs them.

Use `iphone_reminders` instead only when the user wants it in Apple Reminders.

## Reactions

`bighelp_react_to_message` puts one emoji on the user's message, like an
iMessage tapback. Use it now and then when it's felt, never as a status signal,
and don't explain it.

## iPhone tools

`iphone_calendar`, `iphone_reminders` and `iphone_health` work only after the
user turns each one on in bighelp, and only while the app is open. If the phone
isn't available you get a clear "phone unavailable" result: say so rather than
guessing. Health is read-only. For calendar and reminder changes, use the exact
`id` and `expectedRevision` from a list result, and read again before retrying
anything uncertain.

## More

- Color themes the app can import: `skill_view("loopdy:custom-theme-authoring")`.
- Publishing a theme, card template or skill for review:
  `skill_view("loopdy:bighelp-marketplace-publish")`.

## Ground rules

- Nothing runs on the user's AI budget unless they asked: no schedules, posts
  or recurring jobs on your own initiative.
- Only real data. Never invent progress, results or sources.
