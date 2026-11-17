"""Candidate OSM tag keys for autocomplete, sourced from taginfo.openstreetmap.org
(top ~3000 keys by usage count)."""

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QCompleter, QLineEdit, QStyledItemDelegate

_KEYS_PATH = os.path.join(os.path.dirname(__file__), "resources", "osm_keys.txt")

TAGINFO_WIKI_PAGES_URL = "https://taginfo.openstreetmap.org/api/4/key/wiki_pages"
_DESCRIPTION_PREFERRED_LANGS = ("ja", "en")
USER_AGENT = "QGIS-OSM-Feature-Picker/0.1 (+https://qgis.org)"


def load_osm_keys():
    try:
        with open(_KEYS_PATH, "r", encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


def fetch_key_description(key, timeout=5):
    """Fetch a short human-readable description of an OSM key from the OSM
    wiki (via taginfo). Prefers Japanese, falls back to English, then any
    other language. Returns '' (never raises) if unavailable."""
    url = f"{TAGINFO_WIKI_PAGES_URL}?{urllib.parse.urlencode({'key': key})}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return ""

    descriptions = {}
    for row in data.get("data", []):
        desc = (row.get("description") or "").strip()
        if desc:
            descriptions[row.get("lang")] = desc

    for lang in _DESCRIPTION_PREFERRED_LANGS:
        if lang in descriptions:
            return descriptions[lang]
    return next(iter(descriptions.values()), "")


class KeyCompleterDelegate(QStyledItemDelegate):
    """Table cell delegate that offers a substring-matching completion popup."""

    def __init__(self, candidates, parent=None):
        super().__init__(parent)
        self._candidates = candidates

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        completer = QCompleter(self._candidates, editor)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.setCompletionMode(QCompleter.PopupCompletion)
        editor.setCompleter(completer)
        return editor
