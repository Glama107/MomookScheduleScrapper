"""What the client does when Momook says no, with Momook itself stubbed out.

Run with:  python -m tests.test_client
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta

os.chdir(tempfile.mkdtemp(prefix="momook-test-client-"))

import httpx  # noqa: E402

from momook_ics.client import Credentials, MomookClient, MomookError  # noqa: E402

# What Momook answers a request whose session is gone: a 422, not a 401.
SESSION_LOST = {"message": "Not authorized", "errors": "Permission denied"}

WINDOW = (datetime(2026, 8, 1), datetime(2026, 8, 8))


def stub(client: MomookClient, handler) -> list[str]:
    """Point a client at ``handler`` and record what it asks for."""
    seen: list[str] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(f"{request.method} {request.url.path}")
        return handler(request)

    client._http = httpx.Client(
        base_url="https://momook.test", transport=httpx.MockTransport(record)
    )
    return seen


def build() -> MomookClient:
    return MomookClient(
        "https://momook.test", Credentials(username="u", password="p"), timeout=5.0
    )


def test_an_expired_session_is_replaced_rather_than_reported() -> None:
    """The failure that took the deployed service down for a day: Momook ends a
    session — on its own schedule, or because the same user signed in elsewhere
    — and every refresh from then on returns 422 until the process restarts."""
    rejections = [1]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"status": "created"})
        if rejections:
            rejections.pop()
            return httpx.Response(422, json=SESSION_LOST)
        return httpx.Response(200, json=[{"Id": 7}])

    client = build()
    seen = stub(client, handler)

    rows = client.fetch_events(*WINDOW, user_id=1)

    assert rows == [{"Id": 7}], rows
    assert seen == [
        "POST /api/system/auth/session",  # the first sign-in
        "GET /api/schedule",  # rejected: the session is gone
        "POST /api/system/auth/session",  # so sign in again
        "GET /api/schedule",  # and it goes through
    ], seen


# What /api/system/user/identity answers when the session behind it is gone:
# 200, no error, the whole user simply blank.
EMPTY_IDENTITY = {"id": None, "uuid": None, "username": None, "title_full": None}

IDENTITY = {"id": "20937", "username": "u"}


def test_an_empty_identity_is_a_lost_session_too() -> None:
    """The failure that froze the deployed feeds for two weeks. Every other
    endpoint refuses a dead session with a 422; this one answers 200 with the
    user nulled out, so nothing looked wrong — the refresh just could not find
    a user id, and said so again every half hour until it was restarted."""
    identities = [EMPTY_IDENTITY, IDENTITY]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"status": "created"})
        return httpx.Response(200, json=identities.pop(0))

    client = build()
    seen = stub(client, handler)

    assert client.user_id() == 20937, client.user_id()
    assert seen == [
        "POST /api/system/auth/session",  # the first sign-in
        "GET /api/system/user/identity",  # nobody home: the session is gone
        "POST /api/system/auth/session",  # so sign in again
        "GET /api/system/user/identity",  # and there we are
    ], seen


def test_an_empty_identity_is_never_remembered() -> None:
    """And it must not be cached on the way past. Memoising the blank user is
    what turned one expired session into a feed that stayed frozen: the cache
    outlives the session, and only a login clears it."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"status": "created"})
        return httpx.Response(200, json=EMPTY_IDENTITY)

    client = build()
    stub(client, handler)

    try:
        client.user_id()
    except MomookError as exc:
        assert "even after signing in again" in str(exc), exc
    else:
        raise AssertionError("an identity with nobody in it must fail")

    assert client._identity is None, client._identity


def test_a_422_that_is_not_about_the_session_is_not_retried() -> None:
    """422 is also Momook's ordinary validation error. Re-authenticating over
    one would turn a bad request into a login loop."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"status": "created"})
        return httpx.Response(422, json={"errors": ["field.start.invalid"]})

    client = build()
    seen = stub(client, handler)

    try:
        client.fetch_events(*WINDOW, user_id=1)
    except MomookError as exc:
        assert "422" in str(exc), exc
    else:
        raise AssertionError("a validation error must surface, not be retried")

    assert seen.count("POST /api/system/auth/session") == 1, seen


def test_a_session_that_cannot_be_replaced_fails_once() -> None:
    """Re-login is tried once. A password that no longer works must not turn one
    failed refresh into a stream of sign-in attempts."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"status": "created"})
        return httpx.Response(422, json=SESSION_LOST)

    client = build()
    seen = stub(client, handler)

    try:
        client.fetch_events(*WINDOW, user_id=1)
    except MomookError:
        pass
    else:
        raise AssertionError("an unrecoverable session must fail")

    assert seen.count("GET /api/schedule") == 2, seen


def test_the_window_becomes_the_comparisons_momook_expects() -> None:
    captured: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.url)
        if request.url.path.endswith("/session"):
            return httpx.Response(200, json={"status": "created"})
        return httpx.Response(200, json=[])

    client = build()
    stub(client, handler)

    start = datetime(2026, 8, 1, 12, 0)
    client.fetch_events(start, start + timedelta(days=7), user_id=42)

    query = captured[-1].params
    assert query[":Start"] == f"<{int((start + timedelta(days=7)).timestamp())}"
    assert query[":End"] == f">{int(start.timestamp())}"
    assert query["ScheduleEventUser:UserId[]"] == "42"


def main() -> None:
    for name, case in sorted(globals().items()):
        if name.startswith("test_"):
            case()
    print("ok — session recovery, validation errors and schedule filters behave")


if __name__ == "__main__":
    main()
