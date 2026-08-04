"""Editing the .env: quoting, block numbering, adding and removing.

Run with:  python -m tests.test_envfile
"""

from __future__ import annotations

import os
import tempfile

# The settings resolve ".env" relative to the working directory. Step somewhere
# empty, and write the fixtures there.
os.chdir(tempfile.mkdtemp(prefix="momook-test-envfile-"))

from dotenv import dotenv_values  # noqa: E402

from momook_ics import envfile  # noqa: E402
from momook_ics.config import duplicate_keys, parse_accounts  # noqa: E402


def write(name: str, text: str) -> str:
    with open(name, "w", encoding="utf-8") as handle:
        handle.write(text)
    return name


def test_values_survive_the_round_trip() -> None:
    tricky = [
        "simple",
        "p@ss word",
        "with$dollar",
        'with"double',
        "with#hash",
        "with'single",
        "  padded  ",
    ]
    path = write("round-trip.env", "")
    for position, secret in enumerate(tricky, start=1):
        envfile.add_account(path, position, {"USERNAME": f"u{position}", "PASSWORD": secret})

    parsed = dotenv_values(path)
    for position, secret in enumerate(tricky, start=1):
        got = parsed[f"MOMOOK_ACCOUNT_{position}_PASSWORD"]
        assert got == secret, (position, repr(got), repr(secret))

    # What no quoting can express identically to both readers is refused rather
    # than written: a password that means two different things to the service
    # and to the CLI is worse than one that will not save.
    for impossible in ("both'and$", "expand${me}", "two\nlines"):
        try:
            envfile.quote(impossible)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{impossible!r} should not be writable")


def test_a_block_lands_where_nothing_else_is() -> None:
    env = {"MOMOOK_ACCOUNT_1_USERNAME": "a", "MOMOOK_ACCOUNT_3_USERNAME": "c"}
    assert envfile.next_index(env) == 2, "the gap gets filled"
    assert envfile.next_index({}) == 1
    assert envfile.next_index({f"MOMOOK_ACCOUNT_{n}_USERNAME": "x" for n in (1, 2)}) == 3


def test_adding_keeps_what_was_there() -> None:
    path = write(
        "keeps.env",
        "# a comment\nMOMOOK_TIMEZONE=Europe/Lisbon\nMOMOOK_ACCOUNT_1_USERNAME=paul\n",
    )
    backup = envfile.add_account(path, 2, {"LABEL": "Marie", "USERNAME": "marie", "PASSWORD": "x"})

    parsed = dotenv_values(path)
    assert parsed["MOMOOK_TIMEZONE"] == "Europe/Lisbon"
    assert parsed["MOMOOK_ACCOUNT_1_USERNAME"] == "paul"
    assert parsed["MOMOOK_ACCOUNT_2_LABEL"] == "Marie"
    assert "# a comment" in open(path, encoding="utf-8").read()

    assert backup and os.path.exists(backup), "the old file is kept"
    assert os.stat(path).st_mode & 0o077 == 0, "passwords stay unreadable to others"


def test_removing_takes_the_whole_block_and_nothing_else() -> None:
    path = write("removes.env", "MOMOOK_TIMEZONE=Europe/Paris\n")
    envfile.add_account(path, 1, {"LABEL": "Paul", "USERNAME": "paul", "PASSWORD": "x"})
    envfile.add_account(path, 2, {"LABEL": "Marie", "USERNAME": "marie", "PASSWORD": "y"})

    dropped, backup = envfile.drop_account(path, 1)
    assert dropped == 3 and backup, dropped

    parsed = dotenv_values(path)
    assert "MOMOOK_ACCOUNT_1_USERNAME" not in parsed
    assert parsed["MOMOOK_ACCOUNT_2_USERNAME"] == "marie"
    assert parsed["MOMOOK_TIMEZONE"] == "Europe/Paris"
    assert "Paul" not in open(path, encoding="utf-8").read(), "its header comment goes too"

    assert envfile.drop_account(path, 9) == (0, None), "removing nothing changes nothing"


def test_the_unnumbered_account_is_emptied_not_deleted() -> None:
    path = write(
        "unnumbered.env",
        "MOMOOK_USERNAME=me@example.com\nMOMOOK_PASSWORD=secret\nMOMOOK_TIMEZONE=Europe/Paris\n",
    )
    changed, _ = envfile.blank_out(path, ["MOMOOK_USERNAME", "MOMOOK_PASSWORD"])
    assert changed == 2

    parsed = dotenv_values(path)
    assert parsed["MOMOOK_USERNAME"] == ""
    # Still declared, because the globals are what every numbered block inherits.
    assert "MOMOOK_USERNAME" in parsed
    assert parsed["MOMOOK_TIMEZONE"] == "Europe/Paris"


def test_a_block_written_here_parses_back_the_same() -> None:
    """The two halves have to agree: what envfile writes is what config reads."""
    from momook_ics.config import Settings

    path = write("parses.env", "")
    envfile.add_account(
        path,
        4,
        {
            "LABEL": "Marie",
            "USERNAME": "marie@example.com",
            "PASSWORD": "p@ss word",
            "TOTP_SECRET": "JBSWY3DP",
            "FEED_TOKEN": "abc123",
            "ONLY_MY_EVENTS": "false",
        },
    )

    accounts = parse_accounts(dotenv_values(path), Settings(username="", password=""))
    assert len(accounts) == 1, accounts
    marie = accounts[0]
    assert marie.label == "Marie"
    assert marie.password == "p@ss word"
    assert marie.only_my_events is False
    assert marie.index == 4
    assert marie.feed_url("https://momook.example.com/") == (
        "https://momook.example.com/calendar/abc123.ics"
    )


def test_a_pasted_block_that_was_not_renumbered_is_reported() -> None:
    path = write(
        ".env",
        "MOMOOK_ACCOUNT_1_USERNAME=paul\nMOMOOK_TIMEZONE=Europe/Paris\n"
        "MOMOOK_ACCOUNT_1_USERNAME=marie\n",
    )
    assert duplicate_keys(path) == ["MOMOOK_ACCOUNT_1_USERNAME"]
    assert duplicate_keys("nothing-here.env") == []


def main() -> None:
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
    print("ok — quoting, numbering, adding, removing and the duplicate warning behave")


if __name__ == "__main__":
    main()
