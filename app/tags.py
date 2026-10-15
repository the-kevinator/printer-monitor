"""
What each colour tag means: "blue" is the second floor, "orange" is finance.

The meaning belongs to the colour rather than to each printer, so it is named
once here and every printer wearing that colour picks it up. Kept in
`data/tags.json` alongside the other dashboard-managed lists.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from .jsonfile import quarantine, read_json, write_atomic
from .store import PRINTER_COLORS

SCHEMA_VERSION = 1
MAX_LABEL = 40


class TagError(ValueError):
    """A description that cannot be saved, with a reason the user can act on."""


class TagStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._labels: dict[str, str] = {}

    def load(self) -> None:
        with self._lock:
            if not self.path.is_file():
                self._labels = {}
                return
            raw = read_json(self.path)
            if not isinstance(raw, dict):
                quarantine(self.path)
                self._labels = {}
                return
            labels = raw.get("labels") or {}
            self._labels = {
                color: str(text)
                for color, text in labels.items()
                if color in PRINTER_COLORS and isinstance(text, str) and text.strip()
            }

    def labels(self) -> dict[str, str]:
        with self._lock:
            return dict(self._labels)

    def label(self, color: str | None) -> str:
        """The description for a colour, or "" if it has none."""
        with self._lock:
            return self._labels.get(color or "", "")

    def describe(self, color: str | None) -> str:
        """'Blue' or 'Blue: 2nd floor' -- what a person reading a list sees."""
        if color not in PRINTER_COLORS:
            return ""
        label = self.label(color)
        return f"{color.capitalize()}: {label}" if label else color.capitalize()

    def payload(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": name, "hex": hex_value, "label": self._labels.get(name, "")}
                for name, hex_value in PRINTER_COLORS.items()
            ]

    def update(self, labels: dict[str, Any]) -> None:
        """
        Set descriptions for the colours given; a blank one clears it.

        Colours left out of `labels` keep what they had, so the dialog can save
        a single edited row without resending the rest.
        """
        if not isinstance(labels, dict):
            raise TagError("labels must be an object of colour: description")
        cleaned: dict[str, str] = {}
        for color, text in labels.items():
            if color not in PRINTER_COLORS:
                raise TagError(
                    f"{color!r} is not a tag colour; use one of "
                    f"{', '.join(PRINTER_COLORS)}"
                )
            text = "" if text is None else " ".join(str(text).split())
            if len(text) > MAX_LABEL:
                raise TagError(
                    f"The {color} description is {len(text)} characters; "
                    f"keep it to {MAX_LABEL}."
                )
            cleaned[color] = text
        with self._lock:
            merged = dict(self._labels)
            for color, text in cleaned.items():
                if text:
                    merged[color] = text
                else:
                    merged.pop(color, None)
            write_atomic(self.path, {
                "schema_version": SCHEMA_VERSION,
                "saved_at": time.time(),
                "labels": merged,
            })
            self._labels = merged
