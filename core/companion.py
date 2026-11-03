"""Validated, persistent Companion appearance preferences."""
import json
import math
import re
import threading
from pathlib import Path

from core import config
from core.conversation import _atomic_write_json

DEFAULTS = {"preset": "lattice", "accent": "#65dfcc", "size": 1.0,
            "glow": 0.7, "motion": 1.0, "theme": "dark"}
LEGACY_PRESETS = ("crystal", "orb", "rings", "wave")


def validate_preferences(raw):
    if not isinstance(raw, dict):
        raise ValueError("Appearance settings must be an object.")
    result = dict(DEFAULTS)
    for key, value in raw.items():
        if key not in DEFAULTS:
            raise ValueError("Unknown appearance setting.")
        if key == "preset":
            if value in LEGACY_PRESETS:
                value = "lattice"
            elif value != "lattice":
                raise ValueError("Choose Astral lattice.")
        if key == "theme" and value not in ("dark", "light"):
            raise ValueError("Choose light or dark mode.")
        if key == "accent" and (not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value)):
            raise ValueError("Choose a valid accent color.")
        if key in ("size", "glow", "motion"):
            low, high = (0.5, 1.2) if key == "size" else (0, 1)
            if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f"{key} must be between {low} and {high}.")
        result[key] = value
    return result


class Preferences:
    def __init__(self, path=None):
        self.path = Path(path) if path else config.PROJECT_ROOT / "data" / "companion.json"
        self._lock = threading.Lock()

    def get(self):
        with self._lock:
            return self._get()

    def _get(self):
        try:
            return validate_preferences(json.loads(self.path.read_text("utf-8")))
        except (OSError, ValueError):
            return dict(DEFAULTS)

    def update(self, values):
        with self._lock:
            current = self._get()
            if not isinstance(values, dict):
                raise ValueError("Appearance settings must be an object.")
            current.update(values)
            current = validate_preferences(current)
            _atomic_write_json(self.path, current)
            return current
