# Vault link

Hermes keeps saved cards and addresses in a local vault (`agent/vault_store.py`).
Each item has one origin, which is the website it fills in. The vault is
encrypted with a key file in the vault folder of the agent's profile
(`<profile home>/vault/`). Hermes' socket can only add or remove items.

The bighelp app never reads a secret back. So, to use a saved card on a second
website, the person had to type it again. This route copies the item on the
computer instead. The secret never leaves the computer.

## How it works

- The plugin uses the public API of the vault (`list_items`, `resolve_secret`,
  `add_item`) inside the agent's own profile (`profile_scope`). The agent's
  own vault folder and key are the ones used.
- For each item and each origin it adds a new item with the same kind, label
  and secret. The new item has that origin.
- Only `payment` and `address` items are copied. A login is refused to stay
  careful: its authenticator seed, when it uses the `seed|digits|period|algo`
  form, does not go through `add_item` as it was stored. Items
  of 1Password or Bitwarden (`op:…`, `bw:…`) are refused too: only the local
  vault (`vault_…`) is used.
- The plugin checks everything before it writes anything: the shape of the
  request, that each item exists, is local and can be copied, and that each
  origin is valid. A refusal changes nothing.
- A copy that is there already is not made again. It is reported as
  `existing`. An item counts as a copy of the source when the kind, the label
  and the origin are the same and the secret is the same. The plugin compares
  the secrets on the computer and never returns or logs them. So a retry, also
  after a lost answer, is safe and finishes the rest.
- The app sends origins already normalized: `scheme://host[:port]`, with the
  default port left out. The plugin still normalizes them with the
  `normalize_origin` of Hermes (`HTTPS://Shop.Example.org:443/path` becomes
  `https://shop.example.org`). The `origin` in the reply is the form of Hermes.
  Only `http` and `https` are accepted.
- One link runs at a time in the plugin process. A retry that comes while the
  first request still writes waits, then reports the copies as `existing`.
- Messages are fixed text. They never hold a secret value.

## Route

`POST /api/plugins/loopdy/native/vault/link`, feature `native-vault-link-v1`,
with the usual native headers (`If-Match` context ETag and a lowercase
`X-Loopdy-Request-ID`). It is listed in `/native/context` only when the Hermes
has the vault module (`agent.vault_store`). The request can be up to 32 KiB.

Request (no other fields):

```json
{"profile": "nova",
 "item_ids": ["vault_0123456789ab"],
 "origins": ["https://shop.example.org", "https://store.example.net"]}
```

| Field | Rule |
| --- | --- |
| `profile` | Optional. The agent whose vault is used. Missing means the serving agent. The serving agent's own ID works too (`^[a-z0-9][a-z0-9_-]{0,63}$`) |
| `item_ids` | 1 to 10 different IDs of items in that agent's local vault |
| `origins` | 1 to 10 different web addresses (`http://` or `https://`, at most 2,048 characters each). Two spellings of one origin count as the same origin and are refused |

Response (`200`), one row for each item and origin, in request order (items
first, origins inside):

```json
{"copies": [
  {"item_id": "vault_0123456789ab", "origin": "https://shop.example.org",
   "id": "vault_aaaaaaaaaaaa", "status": "created"},
  {"item_id": "vault_0123456789ab", "origin": "https://store.example.net",
   "id": "vault_bbbbbbbbbbbb", "status": "existing"}
]}
```

`id` is the new item, or the item that was there already. When the source
item has the origin itself, `id` is the source's own ID and the status is
`existing`.

## Errors

The body is the usual `{"error": {"code", "message", "retryable", "details"}}`.

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `invalid_request` | A field is wrong, an unknown field, a wrong count, an item or origin twice, or an origin that is not an `http(s)` address. This route uses 400 for the body. |
| 422 | `invalid_request` | The `X-Loopdy-Request-ID` header is missing or not a lowercase UUID. The shared check of the native routes answers this before the body is read |
| 400 | `not_copyable` | The item is a login, or not in the local vault, or has no full card or address details. `details.itemId` names it |
| 404 | `profile_not_found` | The agent no longer exists |
| 404 | `item_not_found` | A local item does not exist in that agent's vault. `details.itemId` names it |
| 413 | `payload_too_large` | The request is bigger than 32 KiB |
| 428 | `context_required` | There is no `If-Match` header. Load the native context first |
| 412 | `context_changed` | The context changed. After the write, `details.copies` lists the copies. A retry reports them as `existing` |
| 500 | `vault_write_failed` | A write failed after some copies were made. `details.copies` has the rows done so far. Retry: the rest is finished |
| 503 | `vault_unavailable` | The Hermes has no vault, or the vault or its key can not be opened. `retryable` is true |
