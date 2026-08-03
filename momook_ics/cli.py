"""Command line entry points, used for testing and one-off exports.

    momook-ics accounts         # list the configured accounts
    momook-ics whoami           # check credentials, print the identity payload
    momook-ics dump             # raw /api/schedule JSON (to refine the mapping)
    momook-ics events           # normalised events, human readable
    momook-ics ics -o out.ics   # write the calendar to a file
    momook-ics serve            # run the HTTP feed for every account

With more than one account configured, every command but ``accounts`` and
``serve`` needs to know which one: ``-a`` takes a label, a block number or a
Momook username.
"""

from __future__ import annotations

import argparse
import json
import sys

from .app import configure_logging
from .client import MomookClient, MomookError
from .config import Account, Settings, find_account, get_settings
from .feed import FeedBuilder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="momook-ics", description=__doc__)
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
        "--urls", action="store_true", help="include each feed path, secret token and all"
    )

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

    # `accounts` is the command you reach for when the configuration is wrong,
    # so it is the one command that must not refuse to run over it.
    if args.command == "accounts":
        return _list_accounts(settings, args.urls)

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
            print(f"    feed       /calendar/{account.feed_token}.ics")
        elif account.feed_token:
            print("    feed       configured (--urls to print it)")
        if gaps:
            print(f"    missing    {', '.join(gaps)}")
        print()

    return 0


def _emit(payload: bytes, output: str | None) -> None:
    if output:
        with open(output, "wb") as handle:
            handle.write(payload)
        print(f"wrote {output} ({len(payload)} bytes)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    raise SystemExit(main())
