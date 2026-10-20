"""Readable notification text for replies that carry bighelp cards.

A card reaches the chat as a ``loopdy-card`` fence of canonical JSON inside the agent's final reply.
The reply alert used to show that fence as it was. Here each card in the reply becomes plain words
instead: the preview its renderer call was given (``notification_text``), else one made from the
card's own fields, else "Sent a card.". The words around the cards are kept where they are.

A preview belongs to exactly one card: the one in this reply whose canonical JSON matches a
renderer result in the same chat's history. Cards rendered but not sent lend nothing, and nothing
is remembered outside Hermes' own history. Replies without a card are returned unchanged.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from typing import Any

NOTIFICATION_TEXT_FIELD = "notification_text"
NOTIFICATION_TEXT_MAX = 300
NOTIFICATION_TEXT_SCHEMA = {"type": "string", "maxLength": NOTIFICATION_TEXT_MAX}
NOTIFICATION_TEXT_HELP = (
    " Optional notification_text: one short plain sentence for the notification, if this card ends up in a "
    "reply that notifies the user; without it one is made from the card, so don't add prose to the reply "
    "just for the notification. Rendering never notifies anyone by itself."
)
NEUTRAL_PREVIEW = "Sent a card."

_FENCE = "```loopdy-card"
_CARD_SCHEMAS = frozenset({"loopdy.card", "loopdy.generative_ui"})
_DELIVERY_SCHEMA = "loopdy.card_delivery"
_MAX_CARD_BYTES = 65_536
_MAX_CARDS = 8
_MAX_HISTORY_MESSAGES = 2_000
_MAX_TOOL_BYTES = 131_072
# Anything that reads as markup, code or a payload rather than a sentence.
_UNSAFE = re.compile(r"```|~~~|<\s*/?\s*[A-Za-z!?]|javascript:|^\s*[\[{]", re.IGNORECASE)


def plain_text(value: Any, limit: int = NOTIFICATION_TEXT_MAX) -> str:
    """One line of plain words, or ``""`` when ``value`` isn't safe to show as text."""
    if not isinstance(value, str):
        return ""
    # Control and invisible formatting characters go; the joiner inside emoji stays.
    text = "".join(" " if unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} and char != "\u200d" else char
                   for char in value[: limit * 4])
    text = " ".join(text.split())
    if not text or _UNSAFE.search(text):
        return ""
    if len(text) > limit:
        cut = text[: limit - 1]
        text = (cut.rsplit(" ", 1)[0] if " " in cut[limit // 2:] else cut).rstrip(" ,;:") + "…"
    return text


def without_notification_text(payload: Any) -> Any:
    """Renderer arguments with the optional preview checked and removed: it never enters the card."""
    if not isinstance(payload, dict) or NOTIFICATION_TEXT_FIELD not in payload:
        return payload
    value = payload[NOTIFICATION_TEXT_FIELD]
    if value is not None and not (isinstance(value, str) and not value.strip()):
        if (not isinstance(value, str) or len(value) > NOTIFICATION_TEXT_MAX
                or plain_text(value) != " ".join(value.split())):
            raise ValueError(f"{NOTIFICATION_TEXT_FIELD} must be plain text of at most "
                             f"{NOTIFICATION_TEXT_MAX} characters")
    return {key: item for key, item in payload.items() if key != NOTIFICATION_TEXT_FIELD}


def reply_preview(reply: Any, history: Any = None) -> Any:
    """``reply`` with each card read as words. Anything without a card comes back unchanged."""
    if not isinstance(reply, str) or len(reply) > 4 * _MAX_CARD_BYTES * _MAX_CARDS:
        return reply
    parts = _parts(reply)
    if not any(kind == "card" for kind, _ in parts):
        return reply
    cards = [_card(value) for kind, value in parts if kind == "card"][:_MAX_CARDS]
    keys = [_key(card) if card else None for card in cards]
    authored = _authored_previews(history, {key for key in keys if key}) if any(keys) else {}
    words: list[str] = []
    shown = 0
    for kind, value in parts:
        if kind == "text":
            text = " ".join(value.split())
        else:
            shown += 1
            if shown > _MAX_CARDS:
                continue
            card, key = cards[shown - 1], keys[shown - 1]
            text = (key and authored.get(key)) or (card and _described(card)) or NEUTRAL_PREVIEW
        if text:
            words.append(text)
    if shown > _MAX_CARDS:
        more = shown - _MAX_CARDS
        words.append(f"And {more} more card{'s' if more > 1 else ''}.")
    return " ".join(words)


def _parts(reply: str) -> list[tuple[str, Any]]:
    """Text runs and cards in order, read the way the app reads them (ChatCardMessageProjection)."""
    stripped = reply.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        # The retired scheduler Inbox path: a reply that is only the card's (or wrapper's) JSON.
        whole = _json(stripped)
        if isinstance(whole, dict) and whole.get("schema") in _CARD_SCHEMAS | {_DELIVERY_SCHEMA}:
            return [("card", whole)]
    lines = reply.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    parts: list[tuple[str, Any]] = []
    text: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        trimmed = line.lstrip(" ")
        indent = len(line) - len(trimmed)
        if indent > 3 or trimmed != _FENCE:
            text.append(line)
            index += 1
            # A card inside an ordinary code block is an example, not a card.
            marker = trimmed[:1]
            if indent <= 3 and marker in {"`", "~"}:
                length = len(trimmed) - len(trimmed.lstrip(marker))
                if length >= 3:
                    while index < len(lines):
                        candidate = lines[index]
                        text.append(candidate)
                        index += 1
                        closing = candidate.lstrip(" ")
                        count = len(closing) - len(closing.lstrip(marker))
                        if (len(candidate) - len(closing) <= 3 and count >= length
                                and not closing[count:].strip()):
                            break
            continue
        if text:
            parts.append(("text", "\n".join(text)))
            text = []
        closing = next((cursor for cursor in range(index + 1, len(lines)) if lines[cursor].strip() == "```"), None)
        if closing is None:  # Never closed: the rest is a broken card.
            parts.append(("card", None))
            break
        parts.append(("card", _json("\n".join(lines[index + 1:closing]))))
        index = closing + 1
    if text:
        parts.append(("text", "\n".join(text)))
    return parts


def _json(raw: Any) -> Any:
    if isinstance(raw, str) and len(raw.encode("utf-8", "ignore")) <= _MAX_TOOL_BYTES:
        try:
            return json.loads(raw)
        except (ValueError, RecursionError):
            return None
    return None


def _card(value: Any) -> dict[str, Any] | None:
    """The card, checked as the app checks it before drawing one. A card it wouldn't draw reads neutral."""
    if isinstance(value, dict) and value.get("schema") == _DELIVERY_SCHEMA:
        value = value.get("card")
    if not isinstance(value, dict) or value.get("schema") not in _CARD_SCHEMAS:
        return None
    from .generative_ui import validate_rendered_envelope

    try:
        return validate_rendered_envelope(value)
    except (ValueError, TypeError, KeyError, RecursionError):
        return None


def _key(card: Any) -> str | None:
    """A card's exact identity: its canonical JSON, the same bytes its fence and its renderer result hold."""
    from .generative_ui import canonical_json

    try:
        return hashlib.sha256(canonical_json(card).encode("utf-8")).hexdigest()
    except (ValueError, RecursionError):
        return None


def _authored_previews(history: Any, wanted: set[str]) -> dict[str, str]:
    """Previews given to the renderer calls whose results are the cards in this reply."""
    if not isinstance(history, list):
        return {}
    calls: dict[str, Any] = {}
    found: dict[str, str] = {}
    for message in history[-_MAX_HISTORY_MESSAGES:]:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or ():
                if not isinstance(call, dict):
                    continue
                function, call_id = call.get("function"), call.get("id") or call.get("call_id")
                if not isinstance(function, dict) or not isinstance(call_id, str):
                    continue
                name, arguments = function.get("name"), function.get("arguments")
                arguments = _json(arguments) if isinstance(arguments, str) else arguments
                if name == "tool_call" and isinstance(arguments, dict):  # Hermes' progressive-disclosure bridge
                    name, arguments = arguments.get("name"), arguments.get("arguments")
                if isinstance(name, str) and name.startswith("bighelp_render_") and isinstance(arguments, dict):
                    calls[call_id] = arguments.get(NOTIFICATION_TEXT_FIELD)
        elif message.get("role") == "tool" and isinstance(message.get("tool_call_id"), str) \
                and message["tool_call_id"] in calls:
            authored = calls.pop(message["tool_call_id"])
            result = _json(message.get("content"))
            if isinstance(result, dict) and result.get("schema") == _DELIVERY_SCHEMA:
                result = result.get("card")
            # A renderer's result is already canonical, so it's compared as it is. The reply's
            # cards it's compared with have passed the app's checks (_card).
            key = _key(result) if isinstance(result, dict) else None
            if key in wanted:
                text = plain_text(authored)
                if text:
                    found[key] = text
                else:
                    found.pop(key, None)  # The latest rendering of this card decides.
    return found


def _described(card: dict[str, Any]) -> str:
    """Words from a checked card's own fields. Identifiers, actions and prompts are never read."""
    title = plain_text(card.get("title"), 120).rstrip(".:")
    try:
        if card["schema"] == "loopdy.card":
            detail = plain_text(card.get("spoken_summary"))
        elif card.get("version") == 1:
            detail = _v1_detail(card)
        else:
            detail = _v2_detail(card["component"], card.get("data") or {})
            if not detail:
                detail = plain_text(card.get("subtitle"))
    except (TypeError, ValueError, KeyError, AttributeError):
        detail = ""
    if title and detail and not detail.casefold().startswith(title.casefold()):
        text = f"{title}: {detail}"
    else:
        text = detail or title
    if text and text[-1] not in ".!?…":
        text += "."
    return plain_text(text)


def _first(items: list[str], total: int) -> str:
    shown = [item for item in items if item][:3]
    if not shown:
        return ""
    if total > len(shown):
        return f"{', '.join(shown)} and {total - len(shown)} more"
    return ", ".join(shown)


def _label(item: Any) -> str:
    if isinstance(item, dict):
        item = next((item[key] for key in ("title", "label", "text", "name") if isinstance(item.get(key), str)), "")
    if isinstance(item, bool) or not isinstance(item, (str, int, float)):
        return ""
    return plain_text(str(item), 80)


def _v1_detail(card: dict[str, Any]) -> str:
    component = card.get("component")
    if component == "summary":
        return plain_text(card.get("body"))
    if component == "metrics":
        metrics = card.get("metrics") or {}
        pairs = [f"{plain_text(name, 60)} {_label(value)}".strip() for name, value in metrics.items()
                 if plain_text(name, 60) and _label(value)]
        return _first(pairs, len(pairs))
    items = card.get("items" if component == "list" else "steps") or []
    return _first([_label(item) for item in items], len(items))


def _number(value: Any, places: int = 0) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    if places:
        return f"{value:,.{places}f}"
    return f"{value:,.0f}" if float(value).is_integer() else f"{value:,.1f}"


def _v2_detail(component: str, data: dict[str, Any]) -> str:
    description = plain_text(data.get("description"))
    if component == "weather_forecast":
        current = data.get("current") or {}
        unit = "°F" if data.get("units") == "us" else "°C"
        condition = plain_text(current.get("condition_label"), 80)
        temperature = _number(current.get("temperature"))
        place = plain_text(data.get("location"), 120)
        reading = ", ".join(part for part in (condition, temperature and temperature + unit) if part)
        return f"{reading} in {place}" if reading and place else reading or place
    if component == "stock_quote":
        symbol, currency = plain_text(data.get("symbol"), 12), plain_text(data.get("currency"), 3)
        price = _number(data.get("price"), 2)
        change = data.get("change_percent")
        moved = ("unchanged" if change == 0 else
                 f"{'up' if change > 0 else 'down'} {abs(change):.2f}%" if _number(change) else "")
        return ", ".join(part for part in (" ".join(p for p in (symbol, price, currency) if p), moved) if part)
    if component == "sports_game":
        teams = data.get("teams") or []
        away = next((team for team in teams if not team.get("home")), None)
        home = next((team for team in teams if team.get("home")), None)
        if not away or not home:
            return ""
        status = data.get("status")
        names = [plain_text(team.get("name"), 80) for team in (away, home)]
        if status in {"live", "final"}:
            score = ", ".join(f"{name} {_number(team.get('score'))}".strip()
                              for name, team in zip(names, (away, home)))
            when = (" ".join(plain_text(data.get(key), 20) for key in ("period_label", "clock")
                             if plain_text(data.get(key), 20)) if status == "live" else "final")
            return f"{score}, {when}" if when else score
        return f"{names[0]} at {names[1]}" + (f", {status}" if status in {"postponed", "cancelled"} else "")
    if component == "checklist":
        items = data.get("items") or []
        done = sum(1 for item in items if isinstance(item, dict) and item.get("completed") is True)
        progress = f"{done} of {len(items)} done" if items else ""
        return f"{progress}. {description}" if description and progress else description or progress
    if component == "selection" and not description:
        count = len(data.get("options") or [])
        return f"{count} options to choose from" if count else ""
    if component == "automation" and not description:
        state = data.get("state") if data.get("state") in {"active", "paused", "completed", "failed"} else ""
        schedule = plain_text(data.get("schedule"), 240)
        return ", ".join(part for part in (state.capitalize(), schedule) if part)
    return description
