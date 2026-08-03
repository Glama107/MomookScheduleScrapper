"""Account parsing: the numbered .env blocks, and what they inherit.

Run with:  python -m tests.test_config
"""

from __future__ import annotations

import os
import tempfile

# Both pydantic-settings and env_layers() resolve ".env" relative to the working
# directory. Step somewhere empty so a real one cannot colour the results.
os.chdir(tempfile.mkdtemp(prefix="momook-test-config-"))

from momook_ics.config import Settings, find_account, parse_accounts  # noqa: E402


def defaults(**overrides) -> Settings:
    base = dict(username="", password="", timezone="Europe/Paris", calendar_name="Momook")
    base.update(overrides)
    return Settings(**base)


def test_numbered_blocks_inherit_the_globals() -> None:
    settings = defaults(timezone="Europe/Lisbon", calendar_name="École", only_my_events=False)
    accounts = parse_accounts(
        {
            "MOMOOK_ACCOUNT_2_USERNAME": "marie@example.com",
            "MOMOOK_ACCOUNT_2_PASSWORD": "s3cret",
            "MOMOOK_ACCOUNT_1_USERNAME": "paul@example.com",
            "MOMOOK_ACCOUNT_1_PASSWORD": "hunter2",
            "MOMOOK_ACCOUNT_1_LABEL": "Paul",
            "MOMOOK_ACCOUNT_1_TIMEZONE": "Europe/Paris",
            "MOMOOK_ACCOUNT_1_ONLY_MY_EVENTS": "TRUE",
            "UNRELATED": "ignored",
        },
        settings,
    )

    assert [a.label for a in accounts] == ["Paul", "account 2"], accounts
    paul, marie = accounts

    # Explicit per-account values win…
    assert paul.timezone == "Europe/Paris" and paul.only_my_events is True
    # …and everything left out falls back to the global one.
    assert marie.timezone == "Europe/Lisbon", marie.timezone
    assert marie.only_my_events is False
    assert marie.calendar_name == "École"
    assert marie.var("PASSWORD") == "MOMOOK_ACCOUNT_2_PASSWORD"


def test_unnumbered_account_leads_when_it_names_a_user() -> None:
    numbered = {
        "MOMOOK_ACCOUNT_1_USERNAME": "paul@example.com",
        "MOMOOK_ACCOUNT_1_PASSWORD": "hunter2",
    }

    without = parse_accounts(numbered, defaults())
    assert [a.username for a in without] == ["paul@example.com"], without

    with_flat = parse_accounts(numbered, defaults(username="me@example.com", password="pw"))
    assert [a.username for a in with_flat] == ["me@example.com", "paul@example.com"]
    assert with_flat[0].var("PASSWORD") == "MOMOOK_PASSWORD"


def test_a_typo_is_refused_rather_than_ignored() -> None:
    for block, expected in (
        ({"MOMOOK_ACCOUNT_1_PASSWORT": "x"}, "MOMOOK_ACCOUNT_1_PASSWORT"),
        ({"MOMOOK_ACCOUNT_1_ONLY_MY_EVENTS": "maybe"}, "true or false"),
    ):
        try:
            parse_accounts(block, defaults())
        except RuntimeError as exc:
            assert expected in str(exc), exc
        else:
            raise AssertionError(f"{block} should have been refused")


def test_serving_demands_one_distinct_token_each() -> None:
    settings = defaults()
    shared = {
        "MOMOOK_ACCOUNT_1_USERNAME": "paul@example.com",
        "MOMOOK_ACCOUNT_1_PASSWORD": "hunter2",
        "MOMOOK_ACCOUNT_1_FEED_TOKEN": "same",
        "MOMOOK_ACCOUNT_2_USERNAME": "marie@example.com",
        "MOMOOK_ACCOUNT_2_PASSWORD": "s3cret",
        "MOMOOK_ACCOUNT_2_FEED_TOKEN": "same",
    }

    settings._accounts = parse_accounts(shared, settings)
    try:
        settings.require_serving()
    except RuntimeError as exc:
        assert "same value" in str(exc), exc
    else:
        raise AssertionError("two accounts sharing a token must not be served")

    missing = dict(shared)
    missing["MOMOOK_ACCOUNT_2_FEED_TOKEN"] = ""
    settings._accounts = parse_accounts(missing, settings)
    try:
        settings.require_serving()
    except RuntimeError as exc:
        assert "MOMOOK_ACCOUNT_2_FEED_TOKEN" in str(exc), exc
    else:
        raise AssertionError("a tokenless account must not be served")

    missing["MOMOOK_ACCOUNT_2_FEED_TOKEN"] = "other"
    settings._accounts = parse_accounts(missing, settings)
    settings.require_serving()  # now complete


def test_accounts_are_addressable_three_ways() -> None:
    accounts = parse_accounts(
        {
            "MOMOOK_ACCOUNT_3_USERNAME": "marie@example.com",
            "MOMOOK_ACCOUNT_3_PASSWORD": "s3cret",
            "MOMOOK_ACCOUNT_3_LABEL": "Marie",
        },
        defaults(),
    )

    for name in ("Marie", "marie", "3", "marie@example.com"):
        assert find_account(accounts, name) is accounts[0], name
    assert find_account(accounts, "1") is None
    assert find_account(accounts, "nobody") is None


def main() -> None:
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
    print("ok — account blocks, inheritance, token rules and lookup behave")


if __name__ == "__main__":
    main()
