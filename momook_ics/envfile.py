"""Editing the .env file: adding and removing account blocks, nothing else.

Adding a person by hand means picking an unused block number, generating a
token, and getting five variable names right — and a block pasted with the
number left as it was replaces the person above it without a word. So the CLI
writes the file instead, and this is the part that touches disk.

Everything here rewrites the whole file atomically and keeps a timestamped
backup: the file holds passwords in the clear, and a half-written one takes a
service down.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime
from typing import Iterable, Mapping

from .config import ENV_PREFIX, account_var, assigned_key

# The order fields are written in, which is also the order they read best in.
BLOCK_ORDER = ("LABEL", "USERNAME", "PASSWORD", "TOTP_SECRET", "FEED_TOKEN")

# Values that need no quoting at all. Deliberately narrow: this file is parsed
# by python-dotenv *and* by docker compose, and the less either has to
# interpret, the fewer ways they can disagree.
_PLAIN = re.compile(r"^[A-Za-z0-9_@%+=:,./-]+$")

_HEADER = "# --- {label} "


def next_index(env: Mapping[str, str]) -> int:
    """The lowest block number not already in use.

    Gaps get filled: removing account 2 and adding somebody hands them the free
    slot rather than growing the file forever.
    """
    used = {parsed[0] for name in env if (parsed := account_var(name)) is not None}
    index = 1
    while index in used:
        index += 1
    return index


def quote(value: str) -> str:
    """A value written so both readers of this file get it back unchanged.

    Single quotes are the safe form: python-dotenv and docker compose both take
    what is between them literally. Double quotes are not — compose expands
    ``$x`` inside them and python-dotenv leaves it alone, so a password would
    mean two different things depending on who read it. They are used only when
    the value contains a single quote, and only when there is no ``$`` to argue
    about.
    """
    if not value:
        return ""
    if "\n" in value or "\r" in value:
        raise ValueError("a value cannot span several lines")
    if "${" in value:
        # No quoting settles this one: compose keeps ${…} inside single quotes,
        # python-dotenv expands it there anyway. Refusing beats writing a
        # password that means one thing to the service and another to the CLI.
        raise ValueError(
            "a value containing '${' cannot be written to the .env unambiguously: "
            "docker compose and python-dotenv disagree on whether to expand it, "
            "whatever the quoting. Change the password."
        )
    if _PLAIN.match(value):
        return value
    if "'" not in value:
        return "'{}'".format(value)
    if "$" in value:
        # Only double quotes are left, and compose expands $x inside them.
        raise ValueError(
            "a value containing both a single quote and a '$' cannot be written to "
            "the .env unambiguously — docker compose and python-dotenv disagree on "
            "what it means. Change the password so it holds only one of the two."
        )
    return '"{}"'.format(value.replace("\\", "\\\\").replace('"', '\\"'))


def render_block(index: int, values: Mapping[str, str], comment: str = "") -> list[str]:
    """The lines one account block is made of."""
    prefix = "{}ACCOUNT_{}_".format(ENV_PREFIX, index)
    label = values.get("LABEL") or "account {}".format(index)

    header = _HEADER.format(label=label)
    lines = ["", header.ljust(78, "-")]
    if comment:
        lines.append("# {}".format(comment))

    keys = list(BLOCK_ORDER) + sorted(set(values) - set(BLOCK_ORDER))
    for key in keys:
        value = values.get(key)
        if value is None:
            continue
        lines.append("{}{}={}".format(prefix, key, quote(value)))
    return lines


def add_account(
    path: str, index: int, values: Mapping[str, str], comment: str = ""
) -> str | None:
    """Append a block to the file. Returns the backup path, if one was made."""
    lines = _read(path)
    while lines and not lines[-1].strip():
        lines.pop()
    return _rewrite(path, lines + render_block(index, values, comment))


def drop_account(path: str, index: int) -> tuple[int, str | None]:
    """Delete a numbered block. Returns how many lines went, and the backup."""
    prefix = "{}ACCOUNT_{}_".format(ENV_PREFIX, index)
    lines = _read(path)

    kept: list[str] = []
    dropped = 0
    for line in lines:
        key = assigned_key(line)
        if key is not None and key.startswith(prefix):
            # The header comment this block was written with goes with it.
            if dropped == 0 and kept and kept[-1].startswith("# ---"):
                kept.pop()
            dropped += 1
            continue
        kept.append(line)

    if not dropped:
        return 0, None
    return dropped, _rewrite(path, _squeeze(kept))


def blank_out(path: str, names: Iterable[str]) -> tuple[int, str | None]:
    """Empty the given variables where they are set, leaving them in place.

    How the unnumbered account is retired: ``MOMOOK_USERNAME`` and friends are
    also the defaults every numbered block inherits, so they are emptied rather
    than deleted — an empty username is what stops it being an account.
    """
    wanted = set(names)
    lines = _read(path)

    changed = 0
    for position, line in enumerate(lines):
        key = assigned_key(line)
        if key in wanted and line.strip() != "{}=".format(key):
            lines[position] = "{}=".format(key)
            changed += 1

    if not changed:
        return 0, None
    return changed, _rewrite(path, lines)


def _read(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().splitlines()
    except FileNotFoundError:
        return []


def _squeeze(lines: list[str]) -> list[str]:
    """Collapse the runs of blank lines a removed block leaves behind."""
    out: list[str] = []
    for line in lines:
        if not line.strip() and out and not out[-1].strip():
            continue
        out.append(line)
    return out


def _rewrite(path: str, lines: list[str]) -> str | None:
    """Replace the file atomically, after copying the old one aside.

    Written next to the target and renamed over it, so a full disk or a killed
    process leaves the original in place rather than half a config.
    """
    backup = _backup(path)

    directory = os.path.dirname(os.path.abspath(path)) or "."
    mode = _mode(path)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=directory, prefix=".env.", suffix=".tmp", delete=False
    )
    try:
        with handle:
            handle.write("\n".join(lines).rstrip("\n") + "\n")
        os.chmod(handle.name, mode)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise
    return backup


def _mode(path: str) -> int:
    """The permissions to write this file with.

    The owner's bits are kept as they were; group and world lose theirs. The
    file holds passwords in the clear, so a rewrite is a chance to narrow it and
    never a chance to widen it — a .env left world-readable stops being so the
    first time an account is added.
    """
    try:
        return (os.stat(path).st_mode & 0o700) or 0o600
    except OSError:
        return 0o600


def _backup(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = "{}.bak-{}".format(path, stamp)
    with open(path, "rb") as source:
        payload = source.read()
    with open(backup, "wb") as target:
        target.write(payload)
    os.chmod(backup, _mode(path))
    return backup
