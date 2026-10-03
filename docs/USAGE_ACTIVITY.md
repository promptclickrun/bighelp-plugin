# Usage activity

The bighelp app's Usage page shows what your agents used: tokens and cost per day, per model, per agent and per
computer. Most of it comes straight from Hermes' own dashboard (`GET /api/analytics/usage` and
`GET /api/analytics/models`, per agent). This plugin adds the parts Hermes doesn't share there:

- **When you use it:** sessions started in each hour of the host's day.
- **Messages:** how many messages those sessions hold.
- **One model's days:** tokens and cost per model per day, so the app can chart a single model.

## Read-only

- Read from the agent's own `state.db`, opened read-only, by when a session started (as Hermes' analytics count).
- Nothing here calls a provider or a model, and no paths, prompts or message text reach the app.
- A database that can't be read answers with no hours and no messages rather than wrong ones.

## For the app

`native-usage-activity-v1` in `/native/context` features.

`POST /api/plugins/loopdy/native/usage/activity` with the usual native headers (`If-Match` context ETag and
`X-Loopdy-Request-ID`) and body `{"agentId": "default", "days": 30}`. `days` is 1 to 365.

```json
{
  "agentId": "default",
  "days": 30,
  "hours": [3, 0, 0, 0, 0, 0, 1, 4, 9, 11, 12, 10, 9, 12, 13, 14, 15, 16, 18, 20, 22, 17, 12, 7],
  "messages": 2417,
  "modelDays": [{"day": "2026-10-01", "model": "claude-opus-5-5", "tokens": 120400, "cost": 1.82}],
  "truncated": false
}
```

- `hours`: 24 counts, midnight first, in the host's time zone. `null` when the database couldn't be read.
- `messages`: `null` when the database couldn't be read or doesn't count messages.
- `modelDays`: tokens are uncached input plus output; cost is Hermes' estimate. At most 4,000 rows; `truncated`
  says the list was cut, and the app then doesn't chart single models.
- Errors: `404 profile_not_found` for an agent the host doesn't have, `422` for a bad body, and
  `503 usage_activity_unavailable` when the feature isn't listed.
