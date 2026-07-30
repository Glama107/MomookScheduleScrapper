"""Helpers for reading loosely-shaped JSON.

Momook's payloads vary in key casing and nesting depending on the endpoint and
the relations requested, so both the API client and the domain model probe a
few spellings. They share these two so the tolerance rules stay in one place.
"""

from __future__ import annotations


def first(mapping: dict, *keys: str) -> object:
    """The first key present with a non-empty value, else ``None``."""
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            return mapping[key]
    return None


def as_int(value: object) -> int | None:
    """``value`` as an int, accepting digit strings, rejecting bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None
