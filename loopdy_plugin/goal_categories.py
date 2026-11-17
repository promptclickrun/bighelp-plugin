"""This computer's goal categories: one list for every agent on it, kept at the Hermes root.

The app's Goals groups goals by these and starts new ones from them. The person changes the list in
the app (``board/categories``), and an agent does the same through ``bighelp_board`` when asked. With
no file the list is the built-in seven. ``other`` is always there and always last, so every goal has
somewhere to land: a goal whose stored category left the list shows under it, and the stored name is
never rewritten, so a category that comes back gets its goals back.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from typing import Any, Iterator
import unicodedata
import uuid

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows hosts keep the built-in list
    fcntl = None

CAPABILITY = "native-goal-categories-v1"
OTHER = "other"
BUILT_IN = (("health", "Health"), ("relationships", "Relationships"), ("finance", "Finance"),
            ("career", "Career"), ("interests", "Interests"), ("productivity", "Productivity"),
            (OTHER, "Other"))
MAX_CATEGORIES = 24
MAX_NAME = 40
MAX_ICON = 16
MAX_FILE_BYTES = 16 * 1024
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}\Z")
# The zero-width joiner inside one emoji (a family, a flag). Written as a number: Hermes' plugin scan
# refuses the character itself in a source file.
_JOINER = chr(0x200D)
_lock = threading.Lock()


class GoalCategoryError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Category:
    id: str
    name: str
    icon: str = ""

    def payload(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "icon": self.icon}


DEFAULTS = tuple(Category(identifier, name) for identifier, name in BUILT_IN)


def path() -> Path:
    """The Hermes root, not a profile's home: one list for the whole computer."""
    try:
        from hermes_constants import get_default_hermes_root
        root = get_default_hermes_root()
    except ImportError:
        from hermes_constants import get_hermes_home
        root = get_hermes_home()
    return Path(root) / "plugin-data" / "loopdy" / "goal-categories.json"


def available() -> bool:
    if fcntl is None or os.name != "posix":
        return False
    try:
        path()
    except Exception:
        return False
    return True


def store() -> "CategoryStore":
    return CategoryStore(path())


def current() -> tuple[Category, ...]:
    """The list in force. Never raises: a board read must not fail over its categories."""
    try:
        return store().list()
    except Exception:
        return DEFAULTS


def _name(value: Any) -> str:
    if not isinstance(value, str):
        raise GoalCategoryError("invalid_category", "A category's name must be text.")
    name = " ".join("".join(ch for ch in value if unicodedata.category(ch)[0] != "C" or ch in " \t\n").split())
    if not name:
        raise GoalCategoryError("invalid_category", "A category needs a name.")
    if len(name) > MAX_NAME:
        raise GoalCategoryError("invalid_category", f"A category's name is at most {MAX_NAME} characters.")
    return name


def _icon(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise GoalCategoryError("invalid_category", "A category's icon must be one emoji.")
    icon = "".join(ch for ch in value.strip() if unicodedata.category(ch)[0] != "C" or ch == _JOINER)
    if len(icon) > MAX_ICON or any(ch.isalnum() or ch.isspace() for ch in icon):
        raise GoalCategoryError("invalid_category", "A category's icon must be one emoji.")
    return icon


def _key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold()


def find(value: Any, categories: tuple[Category, ...]) -> Category | None:
    """The category with this ID or name, whatever its capitals."""
    if not isinstance(value, str) or not value.strip():
        return None
    key = _key(" ".join(value.split()))
    return next((category for category in categories if category.id == key), None) or \
        next((category for category in categories if _key(category.name) == key), None)


class CategoryStore:
    def __init__(self, path: Path):
        self.path = path

    def list(self) -> tuple[Category, ...]:
        try:
            with self.path.open("rb") as file:
                data = file.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return DEFAULTS
        except OSError:
            return DEFAULTS
        return self._parse(data) or DEFAULTS

    def add(self, name: Any, icon: Any = None) -> Category:
        name, icon = _name(name), _icon(icon)
        with self._locked():
            categories = list(self.list())
            if len(categories) >= MAX_CATEGORIES:
                raise GoalCategoryError("too_many_categories", f"A computer keeps at most {MAX_CATEGORIES} goal "
                                                               "categories. Remove one first.")
            self._check_free(name, categories)
            category = Category(self._new_id(name, categories), name, icon)
            # Other stays last.
            categories.insert(len(categories) - 1, category)
            self._save(categories)
            return category

    def update(self, identifier: Any, *, name: Any = None, icon: Any = None) -> Category:
        with self._locked():
            categories = list(self.list())
            index = self._index(identifier, categories)
            before = categories[index]
            if before.id == OTHER and name is not None and _key(_name(name)) != _key(before.name):
                raise GoalCategoryError("category_fixed", "Other keeps its name: it holds every goal without a "
                                                          "category.")
            next_name = before.name if name is None else _name(name)
            self._check_free(next_name, categories[:index] + categories[index + 1:])
            categories[index] = Category(before.id, next_name, before.icon if icon is None else _icon(icon))
            self._save(categories)
            return categories[index]

    def delete(self, identifier: Any) -> Category:
        with self._locked():
            categories = list(self.list())
            index = self._index(identifier, categories)
            if categories[index].id == OTHER:
                raise GoalCategoryError("category_fixed", "Other can't be removed: it holds every goal without a "
                                                          "category.")
            removed = categories.pop(index)
            self._save(categories)
            return removed

    @staticmethod
    def _index(identifier: Any, categories: list[Category]) -> int:
        category = find(identifier, tuple(categories))
        if category is None:
            raise GoalCategoryError("category_not_found", "This computer has no goal category with that name.")
        return categories.index(category)

    @staticmethod
    def _check_free(name: str, others: list[Category]) -> None:
        taken = next((other for other in others if _key(other.name) == _key(name)), None)
        if taken is not None:
            raise GoalCategoryError("category_exists", f"There is already a category named {taken.name}.")

    @staticmethod
    def _new_id(name: str, categories: list[Category]) -> str:
        """The name as an ID, so a built-in category added again ("Health") takes its goals back."""
        base = re.sub(r"[^a-z0-9]+", "-", unicodedata.normalize("NFKD", name).casefold()).strip("-")[:32].strip("-")
        if not base:
            base = "c-" + uuid.uuid4().hex[:8]
        taken = {category.id for category in categories}
        candidate, number = base, 2
        while candidate in taken:
            candidate, number = f"{base}-{number}", number + 1
        return candidate

    @staticmethod
    def _parse(data: bytes) -> tuple[Category, ...] | None:
        if len(data) > MAX_FILE_BYTES:
            return None
        try:
            rows = json.loads(data.decode("utf-8"))["categories"]
        except (ValueError, KeyError, TypeError):
            return None
        if not isinstance(rows, list):
            return None
        categories: list[Category] = []
        for row in rows[:MAX_CATEGORIES]:
            try:
                identifier, name, icon = row["id"], _name(row["name"]), _icon(row.get("icon"))
            except (GoalCategoryError, KeyError, TypeError, AttributeError):
                continue
            if (isinstance(identifier, str) and _ID.fullmatch(identifier) and identifier != OTHER
                    and all(identifier != known.id and _key(name) != _key(known.name) for known in categories)):
                categories.append(Category(identifier, name, icon))
        other = next((row for row in rows if isinstance(row, dict) and row.get("id") == OTHER), None)
        try:
            other_icon = _icon(other.get("icon")) if other else ""
        except GoalCategoryError:
            other_icon = ""
        return (*categories[:MAX_CATEGORIES - 1], Category(OTHER, "Other", other_icon))

    def _save(self, categories: list[Category]) -> None:
        encoded = json.dumps({"schemaVersion": 1, "categories": [category.payload() for category in categories]},
                             ensure_ascii=False, indent=1).encode("utf-8")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".saving-", dir=self.path.parent)
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """One writer at a time, across the plugin's module copies and every Hermes process."""
        if fcntl is None:
            raise GoalCategoryError("categories_unavailable", "Goal categories can't be changed on this computer.")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            descriptor = os.open(self.path.with_name(".goal-categories.lock"), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            raise GoalCategoryError("categories_unavailable",
                                    "Goal categories can't be saved on this computer.") from None
        with _lock:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                yield
            finally:
                os.close(descriptor)
