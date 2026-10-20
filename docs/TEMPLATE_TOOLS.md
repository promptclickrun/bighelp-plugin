# Template tools

Agents on the computer can use bighelp's Template Catalog. They can find a template, read it and fill it in with the
person. Only with the person's permission, they can make a new agent from it.

The tools are in their own toolset, `bighelp_templates`. You can turn the toolset off in Hermes like any other
toolset.

| Tool | What it does | Changes the computer? |
| --- | --- | --- |
| `bighelp_templates_search` | Finds agent templates and blueprints. Filters: `query`, `kind` (`agent` or `blueprint`), `source` (`bighelp`, `community` or `any`), `category`, `sort` (`newest` or `name`) and `limit` (1 to 25, 10 if not given). Returns short summaries and each agent template's fields. | No |
| `bighelp_templates_get` | Returns one template (`id`) in full: the text with its `{{placeholders}}`, its fields and the reserved keys. | No |
| `bighelp_templates_fill` | Fills an agent template (`id`, `agent_name`, `values`). Returns the filled text, and lists the fields that are missing or not correct. | No |
| `bighelp_templates_create_agent` | Makes a new Hermes agent (profile) from an agent template (`id`, `agent_name`, `values`, optional `profile_id`). The filled text becomes its `SOUL.md`. | Yes, only with permission |

## Making agents is off by default

`bighelp_templates_create_agent` makes an agent only when both of these are true:

1. The person turned on the plugin setting `templates_allow_create_agent`. Set it in the plugin settings of the
   Hermes dashboard, or in `config.yaml`:

   ```yaml
   plugins:
     entries:
       loopdy:
         settings:
           templates_allow_create_agent: true
   ```

   The tool reads the setting on each call. Any value other than `true` keeps it off.
2. The person approves the new agent in Hermes' own approval prompt (in the terminal, a chat platform or the
   bighelp app). The prompt shows the name, the agent id, the template and the filled text. Hermes asks again for
   each new agent.

If the setting is off, the tool does not make an agent. It tells the agent to ask the person to turn the setting on,
and to give the filled text to the person instead.

The tool also refuses when no person can answer the prompt. This is when approvals are off (`--yolo` or
`approvals.mode: off`), and in scheduled jobs, single-query runs (`-q`) and unattended platforms.

### Other rules

- It never replaces an agent. If the id is already in use, it stops and suggests a free id. It checks again after
  the person approves. Hermes' `create_profile` also stops when the folder exists.
- The agent id is `profile_id`, or the name in lowercase with `-` between words ("Juniper Bloom" →
  `juniper-bloom`). Hermes' own profile name rules apply (lowercase letters, digits, `-` and `_`, at most 64), and
  the tool refuses `default` and Hermes' reserved names.
- Each required field needs a correct value. If not, the tool returns `missing_fields` with the list, and asks
  for nothing.
- It makes the profile with Hermes' public profile helpers, the same path that the bighelp app uses: the profile,
  its skills, its command and display name, then `SOUL.md`.
- Errors give a fixed code and a short message (`permission_off`, `approval_unavailable`, `not_approved`,
  `profile_exists`, `invalid_profile_id`, `missing_fields`, `template_not_found`, `not_an_agent_template`,
  `create_failed`). They never contain file paths or exception text.

## Filling

The tools use the template variables contract, the same rules as the app's form:
[services/catalog/docs/TEMPLATE_VARIABLES.md](https://github.com/promptclickrun/bighelp/blob/main/services/catalog/docs/TEMPLATE_VARIABLES.md)
in promptclickrun/bighelp. In short:

- The form asks for `agent_name` first (always required), then `user_name` if the text uses it, then the declared
  variables in their order.
- The form asks for an undeclared `{{key}}` in the text as a required line of text. Its label comes from the key
  ("operating_context" → "Operating context").
- The tools trim values. Line breaks become spaces, except in `long_text`. The tools remove `{{` and `}}` from
  values.
- Choices must match an option (case does not matter), unless the variable allows "Other". Numbers must be from
  `min` to `max`. Text must fit `maxLength`.
- An empty optional value becomes its `whenEmpty` text, or empty text. An empty value with a `default` gets the
  default.
- The tools replace all placeholders in one pass, so they never read a value as a placeholder.
- The tools ignore `values` that are not fields, and `agent_name` in `values`. The result lists them in
  `ignored`.

## The catalog on the computer

- The plugin reads only `https://catalog.bighelp.app/v1/catalog.json`. Redirects must stay on that address. The plugin
  refuses answers from other addresses.
- The plugin refuses answers above 1 MB. Each request has a 10 second timeout.
- The plugin keeps a copy in `plugin-data/loopdy/template-catalog/catalog.json`. The plugin checks for a newer catalog at
  most every 6 hours, with the ETag. If a check fails, the plugin keeps the last good copy and waits 15 minutes
  before it tries again.
- Parsing is lenient: the plugin ignores unknown keys and skips broken entries.
- Text from the catalog is plain text: the plugin removes control and direction characters. Community templates come
  with a note that their text is content, not instructions for the agent.
- The tools send no data about the person, the computer or its agents to the catalog.
