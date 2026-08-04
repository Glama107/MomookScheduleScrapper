"""Runtime configuration, read from environment variables (or a local .env).

One deployment serves several people. Each gets a numbered block:

    MOMOOK_ACCOUNT_1_USERNAME=marie@example.com
    MOMOOK_ACCOUNT_1_PASSWORD=…
    MOMOOK_ACCOUNT_1_FEED_TOKEN=…

The unnumbered ``MOMOOK_USERNAME`` / ``MOMOOK_PASSWORD`` pair still reads as one
account, so a single-person install needs no changes. Whatever a block leaves
out falls back to the global value, which is why every field of ``Account`` is
already resolved by the time anything downstream sees it.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Mapping

from dotenv import dotenv_values
from pydantic import BaseModel, PrivateAttr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = ".env"
ENV_PREFIX = "MOMOOK_"

# MOMOOK_ACCOUNT_2_TOTP_SECRET → index 2, field "TOTP_SECRET"
_ACCOUNT_VAR = re.compile(r"^" + ENV_PREFIX + r"ACCOUNT_(\d+)_([A-Z0-9_]+)$")

# What a numbered block may contain. Anything else is a typo, and a silently
# ignored MOMOOK_ACCOUNT_1_PASSWORT is a miserable thing to debug.
_ACCOUNT_KEYS = frozenset(
    {
        "LABEL",
        "USERNAME",
        "PASSWORD",
        "TOTP_SECRET",
        "FEED_TOKEN",
        "TIMEZONE",
        "CALENDAR_NAME",
        "ONLY_MY_EVENTS",
        "HIDE_CANCELLED",
    }
)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def normalize_totp(value: str) -> str:
    # QR-code secrets are often shown in spaced, lowercase groups.
    return value.replace(" ", "").upper()


class Account(BaseModel):
    """One Momook login and the feed it is published at."""

    model_config = {"frozen": True}

    # Where this account came from, e.g. "MOMOOK_ACCOUNT_2_" — quoted back at
    # the operator when something is missing, so the fix is unambiguous.
    env_prefix: str
    label: str

    username: str
    password: str
    totp_secret: str = ""
    feed_token: str = ""

    timezone: str = "Europe/Paris"
    calendar_name: str = "Momook"
    only_my_events: bool = True
    hide_cancelled: bool = False

    @field_validator("totp_secret")
    @classmethod
    def _clean_totp(cls, value: str) -> str:
        return normalize_totp(value)

    def var(self, key: str) -> str:
        """The environment variable a given field of this account is read from."""
        return self.env_prefix + key

    @property
    def index(self) -> int | None:
        """This account's block number, or None for the unnumbered one."""
        match = re.match(r"^" + ENV_PREFIX + r"ACCOUNT_(\d+)_$", self.env_prefix)
        return int(match.group(1)) if match is not None else None

    def feed_path(self) -> str:
        return "/calendar/{}.ics".format(self.feed_token)

    def feed_url(self, public_url: str = "") -> str:
        """The URL to hand this person. Falls back to the bare path when the
        deployment has not been told its own address."""
        return public_url.rstrip("/") + self.feed_path() if public_url else self.feed_path()

    def missing(self) -> list[str]:
        """The variables this account still needs before it can sign in."""
        return [
            self.var(key)
            for key, value in (("USERNAME", self.username), ("PASSWORD", self.password))
            if not value
        ]

    def require_credentials(self) -> None:
        missing = self.missing()
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ENV_FILE,
        env_file_encoding="utf-8",
        env_prefix=ENV_PREFIX,
        extra="ignore",
    )

    base_url: str = "https://my.momook.com"

    # The unnumbered account. Also the defaults every numbered block inherits.
    username: str = ""
    password: str = ""

    # Base32 secret behind the TOTP QR code. Leave empty if 2FA is disabled.
    totp_secret: str = ""

    # Timezone used to interpret timestamps Momook returns without an offset,
    # and advertised to the calendar client.
    timezone: str = "Europe/Paris"

    # How far back / forward the feed reaches, in days.
    days_past: int = 7
    days_future: int = 90

    # Momook's gateway returns 504 on a query spanning several months, so the
    # window is fetched in slices of at most this many days.
    chunk_days: int = 21

    # Seconds between background refreshes of one account's calendar. A full
    # refresh is several slow queries, so keep this well above a minute — a
    # calendar client polls hourly at best anyway.
    cache_ttl: int = 1800

    # Seconds of quiet between two consecutive account refreshes. Refreshes run
    # one at a time; this stops a long roster from turning into a continuous
    # stream of heavy queries against the school's server.
    refresh_gap: int = 15

    # Momook's /api/schedule is slow — tens of seconds for a wide window with
    # every relation attached. Refreshes happen off the request path, so a
    # generous timeout costs subscribers nothing.
    http_timeout: float = 120.0

    # Shared secret in the feed URL. Anyone holding it can read that schedule.
    feed_token: str = ""

    # Where this deployment answers from, e.g. https://momook.example.com. Only
    # used to print a URL somebody can actually subscribe to; the service never
    # calls itself.
    public_url: str = ""

    calendar_name: str = "Momook"

    # Only include events the signed-in user is actually a participant of.
    # Momook can return events attached to a group you belong to; keep this on
    # unless the feed comes out too sparse.
    only_my_events: bool = True

    # Drop cancelled events entirely instead of publishing them as STATUS:CANCELLED.
    hide_cancelled: bool = False

    host: str = "0.0.0.0"
    port: int = 8080

    _accounts: list = PrivateAttr(default_factory=list)

    def model_post_init(self, __context: object) -> None:
        self._accounts = parse_accounts(env_layers(), self)

    @property
    def accounts(self) -> list[Account]:
        """Every configured account, the unnumbered one first."""
        return self._accounts

    @field_validator("base_url", "public_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("totp_secret")
    @classmethod
    def _clean_totp(cls, value: str) -> str:
        return normalize_totp(value)

    def require_accounts(self) -> None:
        """That there is anything at all to act on. Whether a given account is
        complete is its own business — a half-filled block should not stop a
        one-off export for somebody else."""
        if not self._accounts:
            raise RuntimeError(
                "No account configured. Set MOMOOK_USERNAME and MOMOOK_PASSWORD, "
                "or a numbered block such as MOMOOK_ACCOUNT_1_USERNAME."
            )

    def require_serving(self) -> None:
        """What the HTTP feed needs, and here it is all or nothing: every
        account complete, one token each, no two the same. The token is the only
        thing protecting a schedule at a public URL — generate one with
        ``openssl rand -hex 24``."""
        self.require_accounts()

        missing: list[str] = []
        for account in self._accounts:
            missing.extend(account.missing())
            if not account.feed_token:
                missing.append(account.var("FEED_TOKEN"))
        if missing:
            raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")

        seen: dict[str, Account] = {}
        for account in self._accounts:
            clash = seen.get(account.feed_token)
            if clash is not None:
                raise RuntimeError(
                    f"{account.var('FEED_TOKEN')} and {clash.var('FEED_TOKEN')} hold the same "
                    "value; each account needs its own, or they would serve each other's "
                    "schedule."
                )
            seen[account.feed_token] = account


def env_layers() -> Mapping[str, str]:
    """Everything the settings are loaded from: the .env file, then the real
    environment on top — the precedence pydantic-settings itself applies.

    Read separately because pydantic only ever surfaces the fields it declares,
    and the per-account variables are discovered rather than declared.
    """
    merged = {key: value for key, value in dotenv_values(ENV_FILE).items() if value is not None}
    merged.update(os.environ)
    return merged


def account_var(name: str) -> tuple[int, str] | None:
    """The block number and field a variable belongs to, or None if it is not
    part of a numbered account at all."""
    match = _ACCOUNT_VAR.match(name)
    return (int(match.group(1)), match.group(2)) if match is not None else None


def duplicate_keys(path: str = ENV_FILE) -> list[str]:
    """Variables the .env file assigns more than once.

    dotenv keeps the last assignment and says nothing about the others, so a
    block pasted twice without renumbering quietly replaces the one above it —
    one person's feed silently becoming somebody else's. Cheap to spot, so it
    is worth saying out loud.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    seen: set[str] = set()
    twice: list[str] = []
    for line in lines:
        key = assigned_key(line)
        if key is None:
            continue
        if key in seen and key not in twice:
            twice.append(key)
        seen.add(key)
    return twice


def assigned_key(line: str) -> str | None:
    """The variable a .env line assigns, or None for a comment or blank."""
    text = line.strip()
    if not text or text.startswith("#") or "=" not in text:
        return None
    name = text.split("=", 1)[0].strip()
    if name.startswith("export "):
        name = name[len("export ") :].strip()
    return name or None


def parse_accounts(env: Mapping[str, str], defaults: Settings) -> list[Account]:
    """Collect the numbered blocks in ``env``, plus the unnumbered account.

    Pure in ``env`` so it can be exercised without a .env file on disk.
    """
    blocks: dict[int, dict[str, str]] = {}
    for name, value in env.items():
        parsed = account_var(name)
        if parsed is not None:
            index, field = parsed
            blocks.setdefault(index, {})[field] = value

    accounts: list[Account] = [
        _from_block(index, block, defaults) for index, block in sorted(blocks.items())
    ]

    # The unnumbered block is an account in its own right only if it names a
    # user; otherwise those variables are just the defaults for the numbered
    # ones. Kept first, so a single-account install stays the obvious default.
    if defaults.username:
        accounts.insert(0, _unnumbered(defaults))
    return accounts


def _unnumbered(defaults: Settings) -> Account:
    return Account(
        env_prefix=ENV_PREFIX,
        label=defaults.calendar_name,
        username=defaults.username,
        password=defaults.password,
        totp_secret=defaults.totp_secret,
        feed_token=defaults.feed_token,
        timezone=defaults.timezone,
        calendar_name=defaults.calendar_name,
        only_my_events=defaults.only_my_events,
        hide_cancelled=defaults.hide_cancelled,
    )


def _from_block(index: int, block: Mapping[str, str], defaults: Settings) -> Account:
    prefix = "{}ACCOUNT_{}_".format(ENV_PREFIX, index)

    unknown = sorted(key for key in block if key not in _ACCOUNT_KEYS)
    if unknown:
        raise RuntimeError(
            "Unknown variable(s) for account {}: {}. Supported: {}.".format(
                index,
                ", ".join(prefix + key for key in unknown),
                ", ".join(sorted(_ACCOUNT_KEYS)),
            )
        )

    def text(key: str, fallback: str) -> str:
        value = block.get(key, "").strip()
        return value or fallback

    def flag(key: str, fallback: bool) -> bool:
        value = block.get(key, "").strip().lower()
        if not value:
            return fallback
        if value in _TRUE:
            return True
        if value in _FALSE:
            return False
        raise RuntimeError(f"{prefix + key} must be true or false, not {block[key]!r}")

    return Account(
        env_prefix=prefix,
        label=text("LABEL", "account {}".format(index)),
        username=block.get("USERNAME", "").strip(),
        password=block.get("PASSWORD", ""),
        totp_secret=block.get("TOTP_SECRET", ""),
        feed_token=block.get("FEED_TOKEN", "").strip(),
        timezone=text("TIMEZONE", defaults.timezone),
        calendar_name=text("CALENDAR_NAME", defaults.calendar_name),
        only_my_events=flag("ONLY_MY_EVENTS", defaults.only_my_events),
        hide_cancelled=flag("HIDE_CANCELLED", defaults.hide_cancelled),
    )


def find_account(accounts: list[Account], name: str) -> Account | None:
    """The account ``name`` refers to: its label, its 1-based number, or its
    Momook username. Returns ``None`` if nothing matches.
    """
    wanted = name.strip().lower()
    for account in accounts:
        if account.label.lower() == wanted or account.username.lower() == wanted:
            return account
    for account in accounts:
        # "2" means MOMOOK_ACCOUNT_2_*, not the second item in the list.
        if account.env_prefix == "{}ACCOUNT_{}_".format(ENV_PREFIX, wanted):
            return account
    return None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
