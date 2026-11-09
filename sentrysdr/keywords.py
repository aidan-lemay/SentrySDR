"""Each user's alert keywords: whole words or phrases, ignoring case."""
from __future__ import annotations

import re
from functools import lru_cache

MAX_KEYWORDS = 50
APPLIES = ("calls", "messages", "both")


def normalize(word: str) -> str:
    return " ".join(word.split()).lower()


def pattern(words: list[str]) -> str | None:
    """One regular expression for all of a user's words. None when there are none."""
    words = sorted({normalize(w) for w in words if normalize(w)}, key=len, reverse=True)
    if not words:
        return None
    # \\s+ inside phrases, so a line break in a transcript still matches "structure fire".
    body = "|".join(r"\s+".join(re.escape(part) for part in w.split(" ")) for w in words)
    return rf"(?<!\w)(?:{body})(?!\w)"


@lru_cache(maxsize=256)
def _compiled(expression: str) -> re.Pattern:
    return re.compile(expression, re.IGNORECASE)


def regexp(expression: str, value) -> bool:
    """SQLite's REGEXP, for filtering in the database."""
    return value is not None and _compiled(expression).search(str(value)) is not None


def matches(words: list[str], text: str | None) -> list[str]:
    """Which of these words appear in text, as they were typed."""
    if not text:
        return []
    found = []
    for word in words:
        expression = pattern([word])
        if expression and _compiled(expression).search(text):
            found.append(word)
    return found
