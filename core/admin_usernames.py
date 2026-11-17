"""Usernames created from an admin's name and email.

The first candidate is the one shown before save. Later candidates are tried only when
that one is already taken. Nothing here renames an admin who already has a username.
"""

from __future__ import annotations

import re
import unicodedata

_USERNAME = re.compile(r"^[a-z0-9_.-]{3,100}$")
_PIECE = re.compile(r"[^a-z0-9_.-]+")


def _piece(value: str | None) -> str:
    if not value:
        return ""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = _PIECE.sub("", text.lower())
    return text.strip("._-")[:40]


def _accept(stem: str, seen: set[str], ordered: list[str]) -> None:
    stem = stem.strip("._-")
    if len(stem) > 100:
        stem = stem[:100].strip("._-")
    if not _USERNAME.fullmatch(stem) or stem in seen:
        return
    seen.add(stem)
    ordered.append(stem)


def username_candidates(first_name: str | None, last_name: str | None, email: str | None) -> list[str]:
    """Ordered usernames for this person, most specific first. Empty when nothing usable was given."""
    first = _piece(first_name)
    last = _piece(last_name)
    local = ""
    if email and "@" in email:
        local = _piece(email.split("@", 1)[0])

    stems: list[str] = []
    if first and last:
        stems.append(f"{first}.{last}")
        if local:
            stems.append(f"{first}.{last}.{local}")
    elif first and local:
        stems.append(f"{first}.{local}")
        if len(first) >= 3:
            stems.append(first)
        stems.append(local)
    elif last and local:
        stems.append(f"{last}.{local}")
        if len(last) >= 3:
            stems.append(last)
        stems.append(local)
    elif first:
        stems.append(first)
    elif last:
        stems.append(last)
    elif local:
        stems.append(local)

    seen: set[str] = set()
    ordered: list[str] = []
    for stem in stems:
        _accept(stem, seen, ordered)
    if not ordered:
        return []

    preferred = ordered[0]
    for number in range(2, 100):
        suffix = str(number)
        base = preferred[: 100 - len(suffix)].strip("._-")
        _accept(f"{base}{suffix}", seen, ordered)
    return ordered
