"""Command line entry points, used for testing and one-off exports.

    momook-ics whoami           # check credentials, print the identity payload
    momook-ics dump             # raw /api/schedule JSON (to refine the mapping)
    momook-ics events           # normalised events, human readable
    momook-ics ics -o out.ics   # write the calendar to a file
    momook-ics serve            # run the HTTP feed locally
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .client import Credentials, MomookClient, MomookError
from .config import get_settings
from .feed import FeedBuilder


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="momook-ics", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="log HTTP activity")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("whoami", help="verify credentials and print the identity payload")

    dump = sub.add_parser("dump", help="print the raw schedule JSON")
    dump.add_argument("-o", "--output", help="write to a file instead of stdout")
    dump.add_argument("-n", "--limit", type=int, default=0, help="only the first N events")

    sub.add_parser("events", help="print the normalised events")

    ics = sub.add_parser("ics", help="render the calendar")
    ics.add_argument("-o", "--output", help="write to a file instead of stdout")

    sub.add_parser("serve", help="run the HTTP feed")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)-8s %(name)s: %(message)s",
    )

    settings = get_settings()
    try:
        settings.require_credentials()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        return _dispatch(args, settings)
    except MomookError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace, settings) -> int:
    if args.command == "serve":
        from .app import main as serve_main

        serve_main()
        return 0

    if args.command == "whoami":
        with MomookClient(
            settings.base_url,
            Credentials(settings.username, settings.password, settings.totp_secret),
        ) as client:
            identity = client.identity()
            print(json.dumps(identity, indent=2, ensure_ascii=False))
            print(f"\nresolved user id: {client.user_id()}", file=sys.stderr)
        return 0

    builder = FeedBuilder(settings)
    try:
        if args.command == "dump":
            start, end = builder.window()
            rows = builder.fetch_rows()
            if args.limit:
                rows = rows[: args.limit]
            text = json.dumps(rows, indent=2, ensure_ascii=False)
            _emit(text.encode("utf-8"), getattr(args, "output", None))
            print(f"{len(rows)} events between {start:%Y-%m-%d} and {end:%Y-%m-%d}", file=sys.stderr)
            return 0

        if args.command == "events":
            events = builder.fetch_events()
            for event in events:
                print(f"{event.start:%a %d %b %Y %H:%M}–{event.end:%H:%M}  {event.summary}")
                if event.location:
                    print(f"    @ {event.location}")
            print(f"\n{len(events)} events", file=sys.stderr)
            return 0

        if args.command == "ics":
            _emit(builder.build(), getattr(args, "output", None))
            return 0
    finally:
        builder.close()

    raise AssertionError(f"unhandled command {args.command!r}")


def _emit(payload: bytes, output: str | None) -> None:
    if output:
        with open(output, "wb") as handle:
            handle.write(payload)
        print(f"wrote {output} ({len(payload)} bytes)", file=sys.stderr)
    else:
        sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    raise SystemExit(main())
