"""Remembering things between runs: where each document was left, and a few preferences.

Stored as JSON in $XDG_STATE_HOME/acrimonious-reader/state.json.
"""

import json
import os
from pathlib import Path

from gi.repository import GLib

MAX_DOCUMENTS = 500


class StateStore:
    def __init__(self, path=None):
        self.path = Path(path or Path(GLib.get_user_state_dir()) / "acrimonious-reader" / "state.json")
        self._save_id = 0
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        self._documents = data.get("documents", {})
        self._preferences = data.get("preferences", {})

    def document(self, uri):
        return self._documents.get(uri)

    def set_document(self, uri, state):
        self._documents.pop(uri, None)
        self._documents[uri] = state  # most recent last
        while len(self._documents) > MAX_DOCUMENTS:
            del self._documents[next(iter(self._documents))]
        self._schedule_save()

    def preference(self, key, default=None):
        return self._preferences.get(key, default)

    def set_preference(self, key, value):
        self._preferences[key] = value
        self._schedule_save()

    def _schedule_save(self):
        if not self._save_id:
            self._save_id = GLib.timeout_add_seconds(2, self.save)

    def save(self):
        if self._save_id:
            GLib.source_remove(self._save_id)
            self._save_id = 0
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(
                {"documents": self._documents, "preferences": self._preferences}, indent=1))
            os.replace(temporary, self.path)
        except OSError as error:
            print(f"acrimonious-reader: could not save state: {error}")
        return GLib.SOURCE_REMOVE
