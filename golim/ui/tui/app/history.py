import json
from pathlib import Path

from golim.config.app_home import get_app_home


class History:
    MAX_ITEMS = 10

    def __init__(self):
        self._items: list[str] = []
        self._index = -1
        self._load()

    @property
    def _path(self) -> Path:
        return get_app_home() / "data" / "history.json"

    def _load(self):
        p = self._path
        if p.exists():
            try:
                data = json.loads(p.read_text())
                self._items = data[-self.MAX_ITEMS :]
            except (json.JSONDecodeError, OSError):
                self._items = []
        else:
            self._items = []

    def _save(self):
        p = self._path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self._items))

    def add(self, query: str):
        query = query.strip()
        if not query:
            return
        if self._items and self._items[-1] == query:
            return
        self._items.append(query)
        if len(self._items) > self.MAX_ITEMS:
            self._items = self._items[-self.MAX_ITEMS :]
        self._save()
        self._index = -1

    def previous(self) -> str:
        if not self._items:
            return ""
        if self._index == -1:
            self._index = len(self._items) - 1
        elif self._index > 0:
            self._index -= 1
        return self._items[self._index]

    def next(self) -> str:
        if not self._items or self._index == -1:
            return ""
        if self._index < len(self._items) - 1:
            self._index += 1
            return self._items[self._index]
        self._index = -1
        return ""
