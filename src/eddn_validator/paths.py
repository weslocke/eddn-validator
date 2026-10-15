"""Normalised field paths and value types of a message (docs/DESIGN.md §6.3).

Array elements share one path (`Factions[].Name`) and numeric object keys
collapse to `{n}`, so messages of one kind produce comparable path sets.
"""

from __future__ import annotations

import re
from collections import defaultdict

NUMERIC_KEY = re.compile(r"^\d+$")


def value_type(value: object) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "list"
    if isinstance(value, dict):
        return "dict"
    return type(value).__name__


def type_family(t: str) -> str:
    """int and float compare as one family: senders differ in writing 3.0 or 3."""
    return "number" if t in ("int", "float") else t


class Flat:
    """Every path in one message: its types, its scalar values and list lengths."""

    __slots__ = ("types", "scalars", "lengths")

    def __init__(self) -> None:
        self.types: dict[str, set[str]] = defaultdict(set)
        self.scalars: dict[str, list] = defaultdict(list)
        self.lengths: dict[str, list[int]] = defaultdict(list)


def flatten(message: dict) -> Flat:
    flat = Flat()

    def visit(value: object, path: str) -> None:
        t = value_type(value)
        flat.types[path].add(t)
        if t == "dict":
            for key, child in value.items():
                name = "{n}" if NUMERIC_KEY.match(str(key)) else str(key)
                visit(child, f"{path}.{name}" if path else name)
        elif t == "list":
            flat.lengths[path].append(len(value))
            for child in value:
                visit(child, f"{path}[]")
        else:
            flat.scalars[path].append(value)

    if isinstance(message, dict):
        for key, child in message.items():
            visit(child, str(key))
    return flat


SHAPE_SUBS = [
    (re.compile(r"[A-Z]+"), "A"),
    (re.compile(r"[a-z]+"), "a"),
    (re.compile(r"[0-9]+"), "9"),
    (re.compile(r"\s+"), " "),
]


def string_shape(value: str) -> str:
    """A coarse shape: runs of capitals, lowercase, digits and spaces collapse to A/a/9/' '."""
    if value.startswith("$") and value.endswith(";"):
        return "$symbol;"
    s = value[:64]
    for pattern, repl in SHAPE_SUBS:
        s = pattern.sub(repl, s)
    return s
