"""Server-side checks on a spreadsheet snapshot before it is stored.

The snapshot is the editor's own document format and Avora does not interpret
cells. What it does police is links: a snapshot saved by one person is opened
in everyone else's browser, so a `javascript:` (or `data:`, `vbscript:`) link
planted in a cell would run as the person who clicks it. Every link-bearing
string is checked against an allow-list of schemes and blanked otherwise.

Links live in two places in Univer's format: rich-text ranges inside cells
(`...customRanges[].properties.url`) and the hyperlink plugin's resource, which
is itself a JSON document stored as a string (`resources[].data`).
"""

from __future__ import annotations

import json
import re
from typing import Any

# The largest snapshot accepted, in bytes of JSON. Roughly a few hundred
# thousand filled cells; a real tracker is far below this.
MAX_SNAPSHOT_BYTES = 5 * 1024 * 1024

_LINK_KEYS = frozenset({"url", "payload", "href", "link"})
_SAFE_SCHEMES = frozenset({"http", "https", "mailto", "tel"})
_SCHEME = re.compile(r"^([a-z][a-z0-9+.\-]*):")
# Browsers ignore these inside a scheme ("java\tscript:"), so they are stripped
# before the scheme is read.
_IGNORED_IN_SCHEME = re.compile(r"[\x00-\x20\x7f]+")


def is_safe_link(value: str) -> bool:
    """True for http(s)/mailto/tel links, in-sheet anchors and relative paths."""
    normalised = _IGNORED_IN_SCHEME.sub("", value).lower()
    match = _SCHEME.match(normalised)
    return match is None or match.group(1) in _SAFE_SCHEMES


def scrub_links(node: Any) -> Any:
    """A copy of `node` with every unsafe link-bearing string blanked."""
    if isinstance(node, dict):
        return {key: _scrub_entry(key, value) for key, value in node.items()}
    if isinstance(node, list):
        return [scrub_links(item) for item in node]
    return node


def _scrub_entry(key: str, value: Any) -> Any:
    if isinstance(value, str):
        if key in _LINK_KEYS and not is_safe_link(value):
            return ""
        if key == "data":
            return _scrub_embedded_json(value)
        return value
    return scrub_links(value)


def _scrub_embedded_json(value: str) -> str:
    """Plugin resources hold their state as a JSON string; scrub inside it."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    if not isinstance(parsed, (dict, list)):
        return value
    return json.dumps(scrub_links(parsed), separators=(",", ":"))
