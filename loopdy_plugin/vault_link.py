"""Vault link: use a saved card or address on another website, without typing it again.

Hermes' local vault (`agent/vault_store.py`) keeps each item with one origin, and its socket can only add or
remove items. The app never reads a secret back, so it cannot copy one. This module copies the item on the
computer: it reads the secret with the vault's own API and adds a new item with the same kind, label and
secret for each new origin. The secret never leaves this process: it is not returned, logged or put in an
error. Messages here are fixed text for that reason.

Only `payment` and `address` items are copied. A login keeps its identifier in metadata and an optional
authenticator seed in a format that `add_item` does not accept back, so a login is refused.

A copy that already exists (the same kind, label, origin and secret) is reported as `existing` and not made
again. A retry after a failed or lost request therefore finishes the rest and never leaves a duplicate.
"""
from __future__ import annotations

from contextlib import ExitStack
import importlib
import logging
from typing import Any

from .workspace_capabilities import profile_scope

CAPABILITY = "native-vault-link-v1"
MAX_ITEMS = 10
MAX_ORIGINS = 10
# Hermes' local items are named `vault_<hex>`; 1Password (`op:`) and Bitwarden (`bw:`) handles are not ours.
LOCAL_PREFIX = "vault_"
COPYABLE_KINDS = ("payment", "address")
logger = logging.getLogger("hermes.plugins.bighelp")

# Hermes' own names this module relies on.
_STORE_NAMES = ("get_vault_store", "normalize_origin", "VaultError")
_STORE_METHODS = ("list_items", "resolve_secret", "add_item")


class VaultLinkError(Exception):
    """A refusal the app should show. `message` is fixed text and never holds a secret."""

    def __init__(self, status: int, code: str, message: str, details: dict[str, Any] | None = None):
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)


def available() -> bool:
    """Only when Hermes has the vault (older Hermes does not) and its public API has the calls used here."""
    try:
        vault_store = importlib.import_module("agent.vault_store")
        return (all(callable(getattr(vault_store, name, None)) for name in _STORE_NAMES)
                and all(callable(getattr(vault_store.VaultStore, name, None)) for name in _STORE_METHODS)
                and isinstance(getattr(vault_store, "PAYMENT_FIELDS", None), dict)
                and isinstance(getattr(vault_store, "ADDRESS_FIELDS", None), dict)
                and isinstance(getattr(vault_store, "REQUIRED_FIELDS", None), dict))
    except Exception:
        return False


def _unavailable() -> VaultLinkError:
    return VaultLinkError(503, "vault_unavailable", "This computer's vault can't be opened.")


def _normalized_origins(origins: list[str], vault_store: Any) -> list[str]:
    """Hermes' own `normalize_origin`, limited to http and https. Two spellings of one origin are one origin."""
    result: list[str] = []
    for value in origins:
        try:
            origin = vault_store.normalize_origin(value)
        except vault_store.VaultError:
            raise VaultLinkError(400, "invalid_request", "An origin is not a web address that starts with http:// or https://.") from None
        if not origin.startswith(("http://", "https://")):
            raise VaultLinkError(400, "invalid_request", "An origin is not a web address that starts with http:// or https://.")
        if origin in result:
            raise VaultLinkError(400, "invalid_request", "An origin is listed twice.")
        result.append(origin)
    return result


def _secret_of(vault_store: Any, kind: str, secret: dict[str, Any]) -> dict[str, str]:
    """The secret as `add_item` would store it, so a saved copy compares equal to the source."""
    allowed = vault_store.PAYMENT_FIELDS if kind == "payment" else vault_store.ADDRESS_FIELDS
    return {key: str(value) for key, value in secret.items() if key in allowed and str(value or "").strip()}


def link(profile: str | None, item_ids: list[str], origins: list[str]) -> dict[str, Any]:
    """Copy each item to each origin in the agent's own vault. Raises `VaultLinkError` for every refusal.

    `profile` is the agent whose vault is used (its own `vault/` folder and key); None is the process home.
    Everything is checked before the first write, so a refusal changes nothing.
    """
    vault_store = importlib.import_module("agent.vault_store")
    if len(set(item_ids)) != len(item_ids):
        raise VaultLinkError(400, "invalid_request", "An item is listed twice.")
    targets = _normalized_origins(origins, vault_store)
    with ExitStack() as stack:
        if profile is not None:
            try:
                stack.enter_context(profile_scope(profile))
            except ValueError:
                raise VaultLinkError(404, "profile_not_found", "The selected agent no longer exists.") from None
        try:
            store = vault_store.get_vault_store()
            saved = {meta.id: meta for meta in store.list_items()}
            plan = _plan(store, vault_store, saved, item_ids)
        except VaultLinkError:
            raise
        except Exception as error:
            # Hermes' text may carry paths; only the class name is logged.
            logger.error("bighelp vault link failed: vault_unavailable (%s)", type(error).__name__)
            raise _unavailable() from None
        return {"copies": _write(store, vault_store, saved, plan, targets)}


def _plan(store: Any, vault_store: Any, saved: dict[str, Any], item_ids: list[str]) -> list[tuple[Any, dict[str, str]]]:
    """Each source's metadata and secret, after proving every item can be copied."""
    plan = []
    for item_id in item_ids:
        if not item_id.startswith(LOCAL_PREFIX):
            raise VaultLinkError(400, "not_copyable", "Only items saved on this computer can be used on another website.",
                                 {"itemId": item_id})
        meta = saved.get(item_id)
        if meta is None:
            raise VaultLinkError(404, "item_not_found", "A saved item was not found.", {"itemId": item_id})
        if meta.kind not in COPYABLE_KINDS:
            raise VaultLinkError(400, "not_copyable", "Only a saved card or address can be used on another website.",
                                 {"itemId": item_id})
        try:
            secret = _secret_of(vault_store, meta.kind, store.resolve_secret(item_id))
        except vault_store.VaultError:
            # The item was removed between the listing and the read.
            raise VaultLinkError(404, "item_not_found", "A saved item was not found.", {"itemId": item_id}) from None
        if any(field not in secret for field in vault_store.REQUIRED_FIELDS[meta.kind]):
            raise VaultLinkError(400, "not_copyable", "A saved item is missing details, so it can't be copied.",
                                 {"itemId": item_id})
        plan.append((meta, secret))
    return plan


def _write(store: Any, vault_store: Any, saved: dict[str, Any], plan: list[tuple[Any, dict[str, str]]],
           targets: list[str]) -> list[dict[str, str]]:
    copies: list[dict[str, str]] = []
    # Items made by this request count as saved, so two identical sources never make two copies.
    made: dict[str, dict[str, str]] = {}
    secrets: dict[str, dict[str, str]] = {}

    def secret_of(item_id: str) -> dict[str, str] | None:
        if item_id not in secrets:
            try:
                meta = saved[item_id]
                secrets[item_id] = _secret_of(vault_store, meta.kind, store.resolve_secret(item_id))
            except Exception:
                return None
        return secrets[item_id]

    def existing(meta: Any, secret: dict[str, str], origin: str) -> str | None:
        if meta.origin == origin:
            return meta.id
        for other in saved.values():
            if other.origin != origin or other.kind != meta.kind or other.label != meta.label:
                continue
            if other.id in made:
                same = made[other.id] == secret
            else:
                same = secret_of(other.id) == secret
            if same:
                return other.id
        return None

    for meta, secret in plan:
        for origin in targets:
            found = existing(meta, secret, origin)
            if found is not None:
                copies.append({"item_id": meta.id, "origin": origin, "id": found, "status": "existing"})
                continue
            try:
                created = store.add_item(kind=meta.kind, label=meta.label, secret=dict(secret), origin=origin)
            except Exception as error:
                logger.error("bighelp vault link failed: vault_write_failed (%s)", type(error).__name__)
                raise VaultLinkError(500, "vault_write_failed",
                                     "Some copies were made and then the vault could not be written. Try again to finish the rest.",
                                     {"copies": copies}) from None
            saved[created.id] = created
            made[created.id] = secret
            copies.append({"item_id": meta.id, "origin": origin, "id": created.id, "status": "created"})
    return copies
