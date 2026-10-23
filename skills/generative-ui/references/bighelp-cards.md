# bighelp Cards v1

bighelp Cards are a bounded, display-only native card language for new static compositions. Call the direct `bighelp_render_card` tool when it is visible; use the official progressive-disclosure bridge only when it is not. Typed v2 cards remain preferred for already-supported polished use cases until the generic renderer reaches visual parity.

## Safety and lifecycle

For this release, `data_sources` must be an empty array. Put current or computed values directly in the card payload before calling the renderer. The app does not fetch card data from the network. Cards contain no downloaded code, HTML, WebViews, authenticated requests, credentials, or secrets. The card is display-only; use the typed form renderer for input.

## Finite component catalog

Every element has a unique ID, a catalog `type`, bounded `props`, and legal `children`. There are no arbitrary modifiers, colors, fonts, frames, coordinates, or symbols.

| Component | Use | Children |
|---|---|---|
| `card` | titled native container | yes |
| `vstack` | vertical layout | yes |
| `hstack` | horizontal layout | yes |
| `grid` | adaptive two/three-column layout | yes |
| `text` | label, paragraph, caption, heading | no |
| `metric` | prominent labeled value and optional trend | no |
| `badge` | short semantic status | no |
| `progress` | bounded value from zero through max | no |
| `chart` | line, area, or bar series | no |
| `table` | bounded columns and rows | no |
| `list` | reserved for a later live-data release; use the typed list renderer now | one template child |
| `divider` | native divider | no |
| `spacer` | bounded spacing token | no |
| `image` | bundled SF Symbol or app asset | no |

### Object components

`contact`, `place` and `music` show one real person, place or song with fixed action buttons. They are leaves (no children) and take plain values, not `{"literal": ...}` bindings. Use them after you find the thing, instead of a text list; a `vstack` of several is fine. Only a chat whose app draws them accepts them: the call is refused otherwise, so build the card from basic components. Saved templates and `bighelp_render_card_template` stay with basic components.

| Component | Props (required in bold) | Limits |
|---|---|---|
| `contact` | **`name`**, `subtitle`, `phones`, `emails`, `address`, `contact_id` | `name`, `subtitle` up to 200. `phones` and `emails`: up to 5 of `{label, value}` (label optional, up to 40; value up to 200). A phone has only digits, spaces and `+ ( ) - .`. An email looks like `name@example.com`. `address` is `{street, city, region, postal_code, country}`, each up to 100 (an address with every part empty counts as none). `contact_id` (up to 512) is the id `iphone_contacts` gave. |
| `place` | **`name`**, **`latitude`**, **`longitude`**, `subtitle`, `address`, `span_meters` | `name`, `subtitle` up to 200, `address` up to 300. Latitude -90 to 90, longitude -180 to 180. `span_meters` 200 to 50000 (default 1000). |
| `music` | **`kind`**, **`title`**, `artist`, `music_id`, `source`, `url` | `kind` is `song`, `album`, `playlist` or `artist`. `title`, `artist` up to 200. `music_id` (up to 512) and `source` (`catalog` or `library`) come from `iphone_music`. `url` (up to 500) is only `https://music.apple.com/...`. |

```json
{"type":"contact","props":{"name":"Sam Rivera","subtitle":"Sample Cafe","phones":[{"label":"mobile","value":"+1 555 0100"}],"contact_id":"sample-id"},"children":[]}
```

Colors are semantic: `primary`, `secondary`, `positive`, `warning`, `negative`, `accent`, `neutral`. Spacing and typography are finite tokens. Image names are an app-owned allowlist.

### Weather background

A `card` element can show animated weather behind its content. Put `background` beside `props` (never inside them), and only on a `card`:

```json
{"type":"card","props":{"title":"Sample Bay"},"background":{"scene":"rain","intensity":"heavy","time_of_day":"night"},"children":["now"]}
```

`scene` is required: `none`, `clear`, `partly_cloudy`, `overcast`, `rain`, `thunderstorm`, `snow`, `fog`, `wind`. `intensity` is `light`, `moderate` (default) or `heavy`; `time_of_day` is `day` (default), `dusk` or `night`. Use it only when the card is about the weather and the scene matches the conditions you looked up. No other keys are allowed. Older apps show the card without the weather.

## Values and expressions

A value must be a literal in this static-only release:

```json
{"literal":"Open"}
```

JSON Pointer bindings and finite expressions remain reserved for the later live-data release. They cannot be used when `data_sources` is empty.

Format styles are `text`, `integer`, `number`, `currency`, `percent`, `date`, `time`, and `relative_date`, and are locale-aware in the app. Tables, charts, text, and the document have strict finite limits enforced by both plugin and app.

## Worked shapes

### Build health

Use a `grid` of literal `metric`, `progress`, and `badge` values to summarize results already obtained by the agent.

### Project status

Use a `vstack` or `hstack` with literal `text`, `metric`, and `badge` values. Use the typed list or timeline renderer when the presentation requires repeated rows.

### Comparison table

Use a bounded `table` whose columns and rows are fully embedded in the payload.

The original v1 summary/metrics/list/timeline tools and typed v2 renderers remain compatible. Choose one renderer and return its exact validated envelope without Markdown or extra prose.
