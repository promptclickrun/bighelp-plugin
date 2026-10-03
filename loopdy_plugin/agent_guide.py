"""What agents are told about bighelp, and the skills that go with it.

The app starts and reopens its chats with session source ``bighelp``. Hermes
has no built-in hint for that source, so this brief is the agent's only
description of where it is. Without it (or with the old unlabeled chats) Hermes
called the chat its terminal UI, and agents doubted they could send files,
cards or reminders.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger("hermes.plugins.bighelp")

SESSION_SOURCE = "bighelp"
SECTION_ID = "bighelp"

# Hermes caps every plugin's sections together at 8,000 characters.
CHAT_BRIEF = """\
You're talking with the user in bighelp, their iPhone, iPad and Vision Pro app for this Hermes host. \
It's a chat app, not a terminal and not Hermes Desktop.
- Replies render as Markdown: headings, lists, quotes, code, tables and links. To send a file, picture or \
video, put MEDIA:/absolute/path on its own line; it shows in the chat.
- Your questions, approvals and secret requests appear as native prompts on the phone. Ask for passwords \
or API keys only with bighelp_request_secure_input, never in chat.
- Cards: when a card is clearer than prose (weather, a score, a stock, a chart, a dashboard, a form), call \
the matching bighelp_render_* tool and put its display_markdown in your reply once. When the user answers a \
form or picks from a selection card, the app sends it to you as their next message; end your turn and wait \
for it.
- Feed, Ideas and Goals: the bighelp_board tool, only for what the user asked for.
- Reminders, check-ins and scheduled updates: create a Hermes cron job for this agent with deliver \
"local". When it runs, bighelp notifies the user with the run's reply (if they turned on notifications \
for you); "local" stops Hermes also posting it to another channel. Write the job so its reply is the \
message, most important words first. Don't deliver to "loopdy"; that's the retired Link inbox.
- iphone_calendar, iphone_reminders and iphone_health work only after the user turns them on, while the app \
is open.
- Never schedule jobs or posts the user didn't ask for; runs spend their AI budget.\
"""
CHAT_BRIEF_GUIDE = 'Full guide: skill_view("loopdy:bighelp").'

ELSEWHERE_NOTE = """\
The user also has the bighelp app for this agent. To reach them there later (a reminder, a check-in, a \
scheduled update), create a Hermes cron job for this agent with deliver "local"; bighelp notifies them \
with the run's reply if they turned on notifications. For bighelp cards, Feed, Ideas and Goals, see \
skill_view("loopdy:bighelp").\
"""

# Runs nobody is chatting in (or of unknown origin): they follow the prompt of whoever started them.
_UNATTENDED = frozenset({"cron", "subagent", "kanban", "tool", "bot_room", "webhook", "api_server"})

_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
# Folder name, description. Registered as ``loopdy:<folder>``.
SKILLS = (
    ("bighelp", "Start here for anything in the bighelp app: where you are, cards, forms, Feed, Ideas "
                "and Goals, reminders and notifications, secure input and iPhone tools."),
    ("generative-ui", "Every bighelp card tool, with payload rules and examples."),
    ("custom-theme-authoring", "Making color themes the bighelp app can import."),
)


def prompt_section(session: Mapping[str, Any]) -> str:
    platform = str(session.get("platform") or "").strip().lower()
    if platform == SESSION_SOURCE:
        from .people import brief_line
        lines = [CHAT_BRIEF, brief_line(session)]
        if folder := _workspace_line(session):
            lines.append(folder)
        return "\n".join((*lines, CHAT_BRIEF_GUIDE))
    if not platform or platform in _UNATTENDED:
        return ""
    return ELSEWHERE_NOTE


def _workspace_line(session: Mapping[str, Any]) -> str:
    """Where to save files, when the chat starts in Hermes' own folder.

    Hosted Hermes starts chats in its home, and bighelp's Workspace shows the
    workspace folder in it instead, so files saved elsewhere wouldn't show.
    """
    profile = str(session.get("profile_name") or "")
    if not profile:
        return ""
    try:
        from . import workspace_root
        workspace = workspace_root.resolve(profile)
    except Exception as error:  # the brief must never fail a turn
        logger.debug("bighelp workspace brief skipped: %s", type(error).__name__)
        return ""
    if workspace.origin != workspace_root.HERMES_WORKSPACE:
        return ""
    cwd = str(session.get("cwd") or "")
    root = str(workspace.root)
    if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
        return ""
    return (f"- Save files you make for the user in {root} (or a folder inside it). That's the folder the "
            "app's Workspace shows; your working folder holds Hermes' own settings.")


def register(ctx: Any) -> None:
    for name, description in SKILLS:
        ctx.register_skill(name, _SKILLS_ROOT / name / "SKILL.md", description=description,
                           frontmatter={"name": name, "description": description})
    register_section = getattr(ctx, "register_system_prompt_section", None)
    if not callable(register_section):
        logger.info("bighelp chat brief unavailable: Hermes has no plugin prompt sections")
        return
    try:
        register_section(SECTION_ID, prompt_section)
    except ValueError:
        # Already registered in this process (another profile scope or a reload).
        logger.info("bighelp chat brief already registered")
