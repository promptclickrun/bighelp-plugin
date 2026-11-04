# Marketplace facts for the Plugins page

The Hermes dashboard gives apps its plugin catalog at
`/api/dashboard/plugins/catalog`. That catalog comes from
`plugin-catalog.json` on the Hermes docs site. It does not have the data that
the Hermes marketplace page and Hermes Desktop use to sort and show plugins.
They read `plugins.json` from the same site, which adds:

- the GitHub stars of each plugin repository,
- the featured rank that Nous selects,
- the date when Nous added the plugin to the catalog and the date of its last
  change.

This route gives the app that data. It also gives the result of the install
check of this host. For each catalog plugin that Hermes does not install here,
the route sends the words of Hermes. Thus the app disables Install for that
plugin and shows the reason. The app does not compare versions itself.

## How it works

- The plugin reads
  `https://nousresearch.github.io/hermes-agent/docs/api/plugins.json` at most
  one time in 6 hours. It follows redirects only to `https` addresses, and it
  reads at most 16 MB.
- It keeps only the name, stars, featured rank and dates of each plugin, in
  memory and in `plugin-data/loopdy/marketplace-plugins.json`. The plugin
  writes no other data.
- If a read fails, the plugin keeps the last good copy, whatever its age, and
  tries again after one minute. If it has no copy, the route sends no feed
  data.
- The install check is the same check that Hermes does before an install by
  catalog name (`refuse_incompatible_catalog_entry`): a platform that the
  entry does not list, or a `requires_hermes` that this Hermes does not
  satisfy. The plugin runs it on the catalog of Hermes itself
  (`load_catalog_live`), and keeps the result for 10 minutes. A Hermes that
  does not have this check refuses no install, so the route sends no
  refusals.

The phone sends nothing to GitHub. Only the host reads the feed, as it already
does for the catalog.

## Route

`POST /api/plugins/loopdy/native/marketplace/plugins`, feature
`native-marketplace-v1`, with the usual native headers (`If-Match` context
ETag and a lowercase `X-Loopdy-Request-ID`). It changes nothing.

Request:

```json
{}
```

Response:

```json
{"fetchedAt": "2027-01-15T08:00:00Z",
 "plugins": [
   {"name": "lantern", "stars": 4120, "featured": 2,
    "addedAt": "2026-09-20T22:06:49Z", "updatedAt": "2026-11-03T19:49:15Z"},
   {"name": "quill", "stars": 860,
    "installRefusal": "quill needs Hermes >=0.30.0; this is 0.22.6. Update Hermes (`hermes update`) and install again."}
 ]}
```

- `fetchedAt`: when the plugin read the feed, in UTC. It is `null` when the
  plugin has no copy of the feed.
- `plugins`: one row for each plugin that the feed or the install check knows,
  sorted by name. A row has only the fields that have a value.
- `stars`: the GitHub stars of the repository. Plugins in the same repository
  have the same number.
- `featured`: the rank of the plugin in the featured list, starting at 1. The
  app shows a different pick each week, in the same sequence as the
  marketplace.
- `addedAt`, `updatedAt`: UTC times.
- `installRefusal`: the words of Hermes, at most 512 bytes.
- Errors: `422` for a body that is not `{}`, `412 context_changed`, `503
  marketplace_unavailable` when the feature is not in the list.

## Feature

The plugin lists `native-marketplace-v1` when Hermes has its plugin catalog
(`hermes_cli.plugin_catalog`). The feature is in `LATE_FEATURES`, so app
releases that know only 64 features do not get it.

An app without this feature shows the Plugins page from the catalog only, as
before.
