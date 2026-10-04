"""Made-up Template Catalog content for the template tool tests."""
from __future__ import annotations

import copy
import json

COORDINATOR = {
    "id": "release-coordinator",
    "name": "Release Coordinator",
    "role": "{{agent_role}}",
    "vibe": "Calm, organized",
    "description": "Keeps {{user_name}}'s releases on track.",
    "instructions": (
        "You are {{agent_name}}, {{user_name}}'s {{agent_role}}.\n"
        "Where you work: {{operating_context}}\n"
        "Tone: {{tone}}. Sign off as {{agent_name}}."
    ),
    "category": "work",
    "symbol": "shippingbox",
    "source": "bighelp",
    "updatedAt": "2026-09-01T10:00:00Z",
    "variables": [
        {"key": "agent_role", "label": "Role", "type": "text", "required": True,
         "example": "Release coordinator", "help": "What this agent does for you, in a few words.",
         "maxLength": 80},
        {"key": "operating_context", "label": "Where it works", "type": "long_text", "required": False,
         "example": "A two-person studio shipping an iPhone app.", "maxLength": 600,
         "whenEmpty": "General work for the user."},
        {"key": "tone", "label": "Tone", "type": "choice", "options": ["Warm", "Direct", "Playful"],
         "default": "Direct"},
    ],
}

GARDENER = {
    "id": "garden-helper",
    "name": "Garden Helper",
    "role": "Plans the vegetable patch",
    "vibe": "Patient",
    "instructions": "You are {{agent_name}}. Plan {{plot_size}} square meters for {{household}} people.",
    "category": "personal",
    "source": "community",
    "credit": "leafy_dev",
    "updatedAt": "2026-09-20T08:00:00Z",
    "variables": [
        {"key": "plot_size", "label": "Plot size", "type": "number", "min": 1, "max": 500},
    ],
}

# An older hand-made template: {{household}} isn't declared, so it's asked for as required text.
LEGACY = {
    "id": "anchor",
    "name": "Anchor",
    "role": "Steady daily planner",
    "vibe": "Grounded",
    "instructions": "You are {{agent_name}}. Help with {{focus_area}} every morning.",
    "category": "productivity",
    "source": "bighelp",
    "updatedAt": "2026-06-01T00:00:00Z",
}

BLUEPRINT = {
    "id": "feed-productivity-1",
    "board": "feed",
    "category": "productivity",
    "text": "Every Monday, list the three [projects] that need me most.",
    "source": "bighelp",
    "updatedAt": "2026-08-15T00:00:00Z",
}

COMMUNITY_BLUEPRINT = {
    "id": "bp-1234abcd-5678",
    "board": "ideas",
    "category": "research",
    "text": "Find two new [hobby] clubs near [city].",
    "source": "community",
    "credit": "wanderer42",
    "updatedAt": "2026-09-25T00:00:00Z",
}


def catalog(**changes) -> dict:
    document = {
        "schemaVersion": 1,
        "revision": "rev-1",
        "agents": [copy.deepcopy(COORDINATOR), copy.deepcopy(GARDENER), copy.deepcopy(LEGACY)],
        "blueprints": [copy.deepcopy(BLUEPRINT), copy.deepcopy(COMMUNITY_BLUEPRINT)],
    }
    document.update(changes)
    return document


def body(document: dict | None = None) -> bytes:
    return json.dumps(document if document is not None else catalog()).encode("utf-8")
