"""Small, dependency-free text normalization helpers."""

from __future__ import annotations

import re
import unicodedata


def normalize_slug(value: str) -> str:
    """Return a lowercase, ASCII slug with single hyphen separators."""
    ascii_value = (
        unicodedata.normalize("NFKD", value)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", "-", ascii_value).strip("-")
