"""Command line entry points: administering the roster, and one-off exports.

    momook-ics add              # add somebody, and print the URL to hand them
    momook-ics accounts --urls  # the roster, with everyone's feed URL
    momook-ics url NAME         # just one URL, to copy and paste
    momook-ics remove NAME      # take somebody off the roster

    momook-ics whoami           # check credentials, print the identity payload
    momook-ics dump             # raw /api/schedule JSON (to refine the mapping)
    momook-ics events           # normalised events, human readable
    momook-ics ics -o out.ics   # write the calendar to a file
    momook-ics serve            # run the HTTP feed for every account

The first four read and write the ``.env`` in the working directory, so they
run from wherever the deployment keeps it. The rest talk to Momook, and with
more than one account configured they need to know which one: ``-a`` takes a
label, a block number or a Momook username.
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from . import envfile
from .app import configure_logging
from .client import Credentials, MomookClient, MomookError
from .config import (
    ENV_FILE,
    ENV_PREFIX,
    Account,
    Settings,
    duplicate_keys,
    env_layers,
    find_account,
    get_settings,
    normalize_totp,
)
from .feed import FeedBuilder

PROG = "momook-ics"

# Commands that act on the configuration rather than on Momook. They have to
# run over a broken or empty roster: adding the first account is exactly that.
CONFIG_COMMANDS = frozenset({"accounts", "add", "remove", "url"})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log HTTP activity")
    parser.add_argument(
        "-a",
        "--account",
        metavar="NAME",
        help="which account to act on: its label, its number or its username",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    accounts = sub.add_parser("accounts", help="list the configured accounts")
    accounts.add_argument(
        "--urls", action="store_true", help="include each feed URL, secret token and all"
    )

    add = sub.add_parser("add", help="add an account to the .env and print its feed URL")
    add.add_argument("--label", help="the name to know them by; asked for if omitted")
    add.add_argument("--username", help="their Momook username")
    add.add_argument("--password", help="their Momook password; prompted for if omitted")
    add.add_argument(
        "--totp-secret",
        help="base32 2FA secret; prompted for if omitted, empty if 2FA is off",
    )
    add.add_argument("--calendar-name", help="name shown in their calendar app")
    add.add_argument(
        "--no-verify",
        action="store_true",
        help="write the block without trying the credentials first",
    )

    remove = sub.add_parser("remove", help="take an account out of the .env")
    remove.add_argument("name", help="a label, a block number or a username")
    remove.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")

    url = sub.add_parser("url", help="print the feed URL to hand out")
    url.add_argument("name", nargs="?", help="whose; every account if omitted")

    sub.add_parser("whoami", help="verify credentials and print the identity payload")

    dump = sub.add_parser("dump", help="print the raw schedule JSON")
    dump.add_argument("-o", "--output", help="write to a file instead of stdout")
    dump.add_argument("-n", "--limit", type=int, default=0, help="only the first N events")

    sub.add_parser("events", help="print the normalised events")

    ics = sub.add_parser("ics", help="render the calendar")
    ics.add_argument("-o", "--output", help="write to a file instead of stdout")

    sub.add_parser("serve", help="run the HTTP feed")

    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    settings = get_settings()

    if args.command in CONFIG_COMMANDS:
        _warn_about_duplicates()
    else:
        try:
            settings.require_accounts()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

    try:
        return _dispatch(args, settings)
    except MomookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "accounts":
        return _list_accounts(settings, args.urls)

    if args.command == "add":
        return _add_account(settings, args)

    if args.command == "remove":
        return _remove_account(settings, args.name, args.yes)

    if args.command == "url":
        return _print_urls(settings, args.name)

    if args.command == "serve":
        # Checked again in the app's lifespan, which is what a bare
        # `uvicorn momook_ics.app:app` goes through — but a misconfigured block
        # deserves one clean line here rather than a start-up traceback.
        try:
            settings.require_serving()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2

        from .app import main as serve_main

        serve_main()
        return 0

    account = _select(settings, args.account)
    if account is None:
        return 2

    if args.command == "whoami":
        with MomookClient.from_account(account, settings) as client:
            identity = client.identity()
            print(json.dumps(identity, indent=2, ensure_ascii=False))
            print(f"\nresolved user id: {client.user_id()}", file=sys.stderr)
        return 0

    builder = FeedBuilder(account, settings)
    try:
        if args.command == "dump":
            window = builder.window()
            rows = builder.fetch_rows(window)
            if args.limit:
                rows = rows[: args.limit]
            text = json.dumps(rows, indent=2, ensure_ascii=False)
            _emit(text.encode("utf-8"), args.output)
            print(f"{len(rows)} events between {window[0]:%Y-%m-%d} and {window[1]:%Y-%m-%d}", file=sys.stderr)
            return 0

        if args.command == "events":
            events = builder.fetch_events()
            for event in events:
                mark = "ANNULÉ — " if event.cancelled else ""
                print(f"{event.start:%a %d %b %Y %H:%M}–{event.end:%H:%M}  {mark}{event.summary}")
                if event.location:
                    print(f"    @ {event.location}")
            print(f"\n{len(events)} events", file=sys.stderr)
            return 0

        if args.command == "ics":
            _emit(builder.build(), args.output)
            return 0
    finally:
        builder.close()

    raise AssertionError(f"unhandled command {args.command!r}")


def _select(settings: Settings, name: str | None) -> Account | None:
    """The account to act on, or None after explaining what to pass."""
    accounts = settings.accounts
    if name is None:
        if len(accounts) != 1:
            print(
                "error: {} accounts are configured; pick one with -a {}".format(
                    len(accounts), "|".join(known.label for known in accounts)
                ),
                file=sys.stderr,
            )
            return None
        account = accounts[0]
    else:
        account = find_account(accounts, name)
        if account is None:
            print(
                "error: no account named {!r}; known: {}".format(
                    name, ", ".join(known.label for known in accounts)
                ),
                file=sys.stderr,
            )
            return None

    try:
        account.require_credentials()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None
    return account


def _list_accounts(settings: Settings, urls: bool) -> int:
    accounts = settings.accounts
    if not accounts:
        print(
            "No account configured. Set MOMOOK_USERNAME and MOMOOK_PASSWORD, or a "
            "numbered block such as MOMOOK_ACCOUNT_1_USERNAME.",
            file=sys.stderr,
        )
        return 2

    for account in accounts:
        gaps = account.missing()
        if not account.feed_token:
            gaps.append(account.var("FEED_TOKEN"))

        print(f"{account.label}")
        print(f"    user       {account.username or '—'}  ({account.env_prefix}*)")
        print(f"    2FA        {'yes' if account.totp_secret else 'no'}")
        print(f"    calendar   {account.calendar_name}, {account.timezone}")
        print(
            "    filters    only_my_events={}, hide_cancelled={}".format(
                str(account.only_my_events).lower(), str(account.hide_cancelled).lower()
            )
        )
        if urls and account.feed_token:
            print(f"    feed       {account.feed_url(settings.public_url)}")
        elif account.feed_token:
            print("    feed       configured (--urls to print it)")
        if gaps:
            print(f"    missing    {', '.join(gaps)}")
        print()

    if urls and not settings.public_url:
        print(
            f"Set {ENV_PREFIX}PUBLIC_URL to this deployment's address to print URLs "
            "people can subscribe to, rather than bare paths.",
            file=sys.stderr,
        )
    return 0


def _warn_about_duplicates() -> None:
    """A variable assigned twice in the .env keeps only its last value — the way
    a block pasted without renumbering swallows the person above it."""
    twice = duplicate_keys()
    if twice:
        print(
            "warning: {} is set more than once in {}; only the last value counts. "
            "Renumber or delete the duplicate.".format(", ".join(twice), ENV_FILE),
            file=sys.stderr,
        )


def _print_urls(settings: Settings, name: str | None) -> int:
    """The URLs to hand out, one per line, or one bare URL when asked for one."""
    accounts = settings.accounts
    if not accounts:
        print(f"error: no account configured; add one with `{PROG} add`", file=sys.stderr)
        return 2

    if name is not None:
        account = find_account(accounts, name)
        if account is None:
            print(
                "error: no account named {!r}; known: {}".format(
                    name, ", ".join(known.label for known in accounts)
                ),
                file=sys.stderr,
            )
            return 2
        chosen = [account]
    else:
        chosen = accounts

    width = max(len(account.label) for account in chosen)
    for account in chosen:
        if not account.feed_token:
            print(
                f"error: {account.label} has no {account.var('FEED_TOKEN')}",
                file=sys.stderr,
            )
            continue
        url = account.feed_url(settings.public_url)
        # A single account prints the URL alone, so it can be piped or copied.
        print(url if len(chosen) == 1 else f"{account.label.ljust(width)}  {url}")
    return 0


def _add_account(settings: Settings, args: argparse.Namespace) -> int:
    """Ask for what is missing, try the credentials, write the block, print the URL."""
    accounts = settings.accounts

    label = (args.label if args.label is not None else _ask("Name")).strip()
    username = (args.username if args.username is not None else _ask("Momook username")).strip()
    password = args.password if args.password is not None else getpass.getpass("Momook password: ")
    totp = (
        args.totp_secret
        if args.totp_secret is not None
        else getpass.getpass("2FA secret (blank if 2FA is off): ")
    )
    # Stored the way config reads it back, so the file says what is in use.
    totp = normalize_totp(totp.strip())

    if not label or not username or not password:
        print("error: a name, a username and a password are all required", file=sys.stderr)
        return 2

    for existing in accounts:
        if existing.username.lower() == username.lower():
            print(
                "error: {} already signs in as {} ({}). Momook ends a session when the "
                "same user signs in again, so two feeds sharing one login would break "
                "each other's refreshes — remove that one first.".format(
                    existing.label, username, existing.var("USERNAME")
                ),
                file=sys.stderr,
            )
            return 2
        if existing.label.lower() == label.lower():
            print(
                f"error: {existing.label} already goes by that name ({existing.env_prefix}*); "
                "pick another so `-a` can tell them apart.",
                file=sys.stderr,
            )
            return 2

    only_my_events = settings.only_my_events
    if not args.no_verify:
        only_my_events = _verify(settings, username, password, totp)

    values = {
        "LABEL": label,
        "USERNAME": username,
        "PASSWORD": password,
        "TOTP_SECRET": totp,
        # One per person: the whole of what keeps one subscriber out of
        # another's schedule.
        "FEED_TOKEN": secrets.token_hex(24),
    }
    if args.calendar_name:
        values["CALENDAR_NAME"] = args.calendar_name
    if only_my_events != settings.only_my_events:
        values["ONLY_MY_EVENTS"] = str(only_my_events).lower()

    index = envfile.next_index(env_layers())
    try:
        backup = envfile.add_account(
            ENV_FILE,
            index,
            values,
            comment="added {}".format(datetime.now().strftime("%Y-%m-%d")),
        )
    except (OSError, ValueError) as exc:
        # A read-only mount, or a password no quoting can express unambiguously.
        print(f"error: {ENV_FILE} not written: {exc}", file=sys.stderr)
        return 1

    account = _preview(index, values, settings)
    print(f"\n✓ {label} written to {ENV_FILE} as {account.env_prefix}*")
    if backup:
        print(f"  previous file kept as {backup}")
    print(f"\n  {account.feed_url(settings.public_url)}\n")
    if not settings.public_url:
        print(
            f"That is the path only — set {ENV_PREFIX}PUBLIC_URL to this deployment's "
            "address to get the full URL."
        )
    print("The service serves it once it restarts.")
    return 0


def _preview(index: int, values: dict, settings: Settings) -> Account:
    """The account the block just written will parse back into."""
    return Account(
        env_prefix="{}ACCOUNT_{}_".format(ENV_PREFIX, index),
        label=values["LABEL"],
        username=values["USERNAME"],
        password=values["PASSWORD"],
        totp_secret=values["TOTP_SECRET"],
        feed_token=values["FEED_TOKEN"],
        timezone=settings.timezone,
        calendar_name=values.get("CALENDAR_NAME", settings.calendar_name),
    )


def _verify(settings: Settings, username: str, password: str, totp_secret: str) -> bool:
    """Sign in as this person, and report whether their events are their own.

    Two things are worth knowing before their credentials are written down: that
    they work at all — a mistyped password would otherwise surface half an hour
    later as a failed background refresh — and whether the school hangs lessons
    off the person or off their training group. The latter is what
    ``ONLY_MY_EVENTS`` decides, and getting it wrong is the usual reason a brand
    new feed comes out empty.
    """
    credentials = Credentials(username=username, password=password, totp_secret=totp_secret)
    with MomookClient(settings.base_url, credentials, timeout=settings.http_timeout) as client:
        client.login()
        user_id = client.user_id()
        print(f"  signed in as {username} (user {user_id})")

        start = datetime.now(ZoneInfo(settings.timezone))
        end = start + timedelta(days=settings.chunk_days)
        span = f"the next {settings.chunk_days} days"

        mine = client.fetch_events(start, end, user_id=user_id)
        if mine:
            print(f"  {len(mine)} events in {span}")
            return True

        everyone = client.fetch_events(start, end, user_id=None)
        if everyone:
            print(
                f"  nothing booked in their name, but {len(everyone)} events they can see "
                "— publishing those (ONLY_MY_EVENTS=false)"
            )
            return False

        print(
            f"  credentials work, but nothing is scheduled in {span}; the feed stays "
            "empty until the school books something"
        )
        return settings.only_my_events


def _remove_account(settings: Settings, name: str, assume_yes: bool) -> int:
    accounts = settings.accounts
    account = find_account(accounts, name)
    if account is None:
        print(
            "error: no account named {!r}; known: {}".format(
                name, ", ".join(known.label for known in accounts) or "none"
            ),
            file=sys.stderr,
        )
        return 2

    if not assume_yes:
        answer = _ask(f"Remove {account.label} ({account.username}) from {ENV_FILE}? [y/N]")
        if answer.strip().lower() not in ("y", "yes"):
            print("nothing removed", file=sys.stderr)
            return 1

    index = account.index
    try:
        if index is None:
            # The unnumbered account doubles as the defaults every block
            # inherits, so its variables are emptied rather than deleted.
            changed, backup = envfile.blank_out(
                ENV_FILE, [ENV_PREFIX + key for key in envfile.BLOCK_ORDER]
            )
        else:
            changed, backup = envfile.drop_account(ENV_FILE, index)
    except OSError as exc:
        print(f"error: {ENV_FILE} not written: {exc}", file=sys.stderr)
        return 1

    if not changed:
        print(
            f"error: nothing to remove from {ENV_FILE} — {account.env_prefix}* is set in the "
            "environment itself, not in the file.",
            file=sys.stderr,
        )
        return 1

    print(f"✓ {account.label} removed from {ENV_FILE} ({changed} lines)")
    if backup:
        print(f"  previous file kept as {backup}")
    print("Their URL stops working once the service restarts.")
    return 0


def _ask(prompt: str) -> str:
    try:
        return input(f"{prompt}: ")
    except EOFError:
        # Piped in with a flag left out: say which one rather than crash.
        return ""


def _emit(payload: bytes, output: str | None) -> None:
    if output:
        with open(output, "wb") as handle:
            handle.write(payload)
        print(f"wrote {output} ({len(payload)} bytes)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    raise SystemExit(main())
