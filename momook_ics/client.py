"""HTTP client for the private Momook REST API.

Everything here mirrors what the official React front-end at
``/react-ui`` does: a PHP session cookie obtained from
``POST /api/system/auth/session``, then plain JSON GETs with an
``X-Requested-With: XMLHttpRequest`` header.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime

import httpx
import pyotp

from ._shape import as_int, first

log = logging.getLogger(__name__)

# Values of the ``status`` field returned by the session endpoint.
STATUS_CREATED = "created"
STATUS_PASSWORD_IS_NOT_SET = "password_is_not_set"
STATUS_AUTH_2FA = "auth2fa"
STATUS_VALID = "valid"

# Relations the front-end asks for on the "my schedule" view. Without these the
# response only carries bare foreign keys and the events are unreadable.
USER_EVENT_RELATIONS = [
    "ScheduleEventRequest.TrainingTopic",
    "ScheduleEventRequest.Training",
    "ScheduleEventAttendee",
    "ScheduleEventUser",
    "ScheduleEventUser.User",
    "ScheduleEventTrainingGroup",
    "ScheduleEventTrainingGroup.TrainingGroup",
    "ScheduleEventTrainingGroup.TrainingGroupCrew",
    "ScheduleEventRequest",
    "ScheduleEventRequest.RequestEntity",
    "ScheduleEventRequest.RequestEntity.CoreClient",
    "ScheduleEventClassroom",
    "ScheduleEventClassroom.ClassroomEntity",
    "ScheduleEventSimulator",
    "ScheduleEventSimulator.SimulatorEntity",
    "ScheduleEventSimulator.AircraftModel",
    "ScheduleEventSimulator.AircraftEngine",
    "ScheduleEventSimulator.AircraftType",
    "ScheduleEventSimulator.SlotType",
    "ScheduleEventAircraft",
    "ScheduleEventAircraft.AircraftEntity",
    "ScheduleEventClient",
    "ScheduleEventClient.Client",
    "ScheduleEventMetadata",
    "CreatedBy",
]

# ``empty`` is not a real TimeStatus; the front-end passes it so events that
# have not been given a status yet are returned too.
TIME_STATUSES = ["empty", "started", "stopped", "paused", "cancelled", "planning"]

# Invariant across every schedule query.
_STATIC_SCHEDULE_PARAMS = [("Rel[]", rel) for rel in USER_EVENT_RELATIONS] + [
    (":TimeStatus[]", status) for status in TIME_STATUSES
]


class MomookError(RuntimeError):
    """Any failure while talking to Momook."""


class MomookAuthError(MomookError):
    """Login was rejected, or the session could not be established."""


class MomookOverloadError(MomookError):
    """Momook gave up on the query — its gateway timed out or bailed out.

    Distinct from the other failures because it is the one worth retrying with
    a narrower window; everything else stays fatal for the refresh.
    """


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str
    totp_secret: str = ""


class MomookClient:
    """Thread-safe, session-reusing client. Re-authenticates on demand."""

    def __init__(
        self,
        base_url: str,
        credentials: Credentials,
        *,
        timeout: float = 30.0,
    ) -> None:
        self._credentials = credentials
        self._timeout = timeout
        self._lock = threading.Lock()
        self._authenticated = False
        self._identity: dict | None = None
        self._http = httpx.Client(
            base_url=base_url,
            timeout=timeout,
            follow_redirects=False,
            # One client per account, and a deployment may hold a dozen. Queries
            # go out one at a time and minutes apart, so a wide pool would only
            # keep idle sockets alive: hold at most one between refreshes.
            limits=httpx.Limits(
                max_connections=2, max_keepalive_connections=1, keepalive_expiry=30.0
            ),
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/plain, */*",
                "User-Agent": "momook-ics/0.1 (+https://github.com/)",
            },
        )

    @classmethod
    def from_account(cls, account, settings) -> "MomookClient":
        """The one way to turn configuration into a client."""
        return cls(
            settings.base_url,
            Credentials(
                username=account.username,
                password=account.password,
                totp_secret=account.totp_secret,
            ),
            timeout=settings.http_timeout,
        )

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "MomookClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- authentication ----------------------------------------------------

    def login(self) -> dict:
        """Create a session. Handles the two-step TOTP challenge."""
        with self._lock:
            login_data: dict[str, object] = {
                "username": self._credentials.username,
                "password": self._credentials.password,
            }
            payload = self._post_session(login_data)
            status = payload.get("status")

            if status == STATUS_AUTH_2FA:
                if not self._credentials.totp_secret:
                    raise MomookAuthError(
                        "Momook asked for a 2FA code but no TOTP secret is configured "
                        "(set MOMOOK_TOTP_SECRET)."
                    )
                login_data["auth2fa"] = self._totp_code()
                login_data["auth2fa_remember"] = True
                payload = self._post_session(login_data)
                status = payload.get("status")

            if status == STATUS_PASSWORD_IS_NOT_SET:
                raise MomookAuthError(
                    "Momook wants this account to set a password before it can be used. "
                    "Log in once through the web UI, then retry."
                )
            if status not in (STATUS_CREATED, STATUS_VALID):
                raise MomookAuthError(f"Unexpected authentication status: {status!r}")

            self._authenticated = True
            self._identity = None
            log.info("Authenticated against Momook as %s", self._credentials.username)
            return payload

    def _totp_code(self) -> str:
        try:
            return pyotp.TOTP(self._credentials.totp_secret).now()
        except Exception as exc:  # noqa: BLE001 - surfaced verbatim to the operator
            raise MomookAuthError(f"Could not derive a TOTP code: {exc}") from exc

    def _post_session(self, login_data: dict[str, object]) -> dict:
        try:
            response = self._http.post(
                "/api/system/auth/session",
                json={"loginData": login_data, "createPasswordData": None},
            )
        except httpx.HTTPError as exc:
            raise MomookError(f"Could not reach Momook: {exc}") from exc

        # Momook answers bad credentials and bad 2FA codes with 422 and an
        # `errors` array of i18n keys, e.g. "page.login.error.invalidCredentials".
        if response.status_code in (400, 401, 403, 422):
            raise MomookAuthError(
                f"Momook rejected the login (HTTP {response.status_code}): "
                f"{_error_summary(response)}"
            )
        if response.status_code >= 400:
            raise MomookError(
                f"Login failed with HTTP {response.status_code}: {_short_body(response)}"
            )
        return _json(response)

    def _ensure_authenticated(self) -> None:
        if not self._authenticated:
            self.login()

    # -- requests ----------------------------------------------------------

    def _get(self, path: str, params: list[tuple[str, str]] | None = None) -> object:
        """GET with one transparent re-login if the session has expired."""
        self._ensure_authenticated()
        response = self._raw_get(path, params)

        if _is_session_lost(response):
            log.info("Momook session expired; re-authenticating")
            self.login()
            response = self._raw_get(path, params)

        if response.status_code in (502, 503, 504):
            raise MomookOverloadError(
                f"GET {path}: Momook returned HTTP {response.status_code} "
                f"(query too large or backend busy)"
            )
        if response.status_code >= 400:
            raise MomookError(
                f"GET {path} failed with HTTP {response.status_code}: {_short_body(response)}"
            )
        return _json(response)

    def _raw_get(self, path: str, params: list[tuple[str, str]] | None) -> httpx.Response:
        try:
            return self._http.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise MomookOverloadError(f"GET {path}: timed out after {self._timeout}s") from exc
        except httpx.HTTPError as exc:
            raise MomookError(f"GET {path} failed: {exc}") from exc

    # -- API surface -------------------------------------------------------

    def identity(self) -> dict:
        """The signed-in user, as returned by ``/api/system/user/identity``."""
        if self._identity is None:
            data = self._get("/api/system/user/identity")
            if not isinstance(data, dict):
                raise MomookError(f"Unexpected identity payload: {type(data).__name__}")
            self._identity = data
        return self._identity

    def user_id(self) -> int:
        """The numeric user id used to filter schedule events."""
        identity = self.identity()
        user_id = _find_user_id(identity)
        if user_id is None:
            raise MomookError(
                "Could not locate a user id in the identity payload. "
                "Run `momook-ics whoami` and share the output so the lookup can be fixed."
            )
        return user_id

    def fetch_events(
        self, start: datetime, end: datetime, *, user_id: int | None
    ) -> list[dict]:
        """Schedule events overlapping ``[start, end]``.

        Momook filters with two half-open comparisons on unix timestamps: the
        event must start before the window ends and end after it begins. Pass
        ``user_id`` to have the server return only that person's events, or
        ``None`` for everything the account can see.
        """
        params: list[tuple[str, str]] = [
            (":Start", f"<{int(end.timestamp())}"),
            (":End", f">{int(start.timestamp())}"),
        ]
        if user_id is not None:
            params.append(("ScheduleEventUser:UserId[]", str(user_id)))
        params += _STATIC_SCHEDULE_PARAMS

        data = self._get("/api/schedule", params)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            # Some list endpoints wrap the rows one level down.
            for value in data.values():
                if isinstance(value, list):
                    return value
            raise MomookError(
                f"/api/schedule returned an object with no row list; keys: {sorted(data)}"
            )
        raise MomookError(f"Unexpected /api/schedule payload: {type(data).__name__}")


# -- helpers ---------------------------------------------------------------


def _json(response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError as exc:
        raise MomookError(
            f"Expected JSON from {response.request.url}, got {response.headers.get('content-type')!r}: "
            f"{_short_body(response)}"
        ) from exc


def _error_summary(response: httpx.Response) -> str:
    """Prefer Momook's structured error keys over the raw body."""
    try:
        payload = response.json()
    except ValueError:
        return _short_body(response)
    if not isinstance(payload, dict):
        return _short_body(response)
    errors = payload.get("errors")
    if isinstance(errors, list) and errors:
        return ", ".join(str(item) for item in errors)
    message = payload.get("message")
    if isinstance(message, str) and message:
        return message
    return _short_body(response)


def _short_body(response: httpx.Response, limit: int = 300) -> str:
    text = response.text.strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _is_login_redirect(response: httpx.Response) -> bool:
    if not response.has_redirect_location:
        return False
    return "login" in response.headers.get("location", "").lower()


# Momook's answer to a request whose PHP session is gone. It does not use 401:
# `{"message": "Not authorized", "errors": "Permission denied"}` under a 422 —
# the same status it returns for an ordinary validation error, which is why the
# body has to be read to tell the two apart.
_SESSION_LOST = ("not authorized", "permission denied")


def _is_session_lost(response: httpx.Response) -> bool:
    """Whether this response means "log in again" rather than "you got it wrong".

    Sessions do expire — and signing the same Momook user in elsewhere ends this
    one immediately — so a long-running feed hits this routinely. Recognising it
    is the difference between one re-login and a service that stays broken until
    it is restarted.
    """
    if response.status_code in (401, 403) or _is_login_redirect(response):
        return True
    if response.status_code != 422:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    body = " ".join(str(payload.get(key, "")) for key in ("message", "errors")).lower()
    return any(marker in body for marker in _SESSION_LOST)


def _find_user_id(identity: dict) -> int | None:
    """Pull the user id out of the identity payload, tolerating key variations."""
    for container in (identity, identity.get("user"), identity.get("User"), identity.get("data")):
        if isinstance(container, dict):
            user_id = as_int(first(container, "Id", "id", "UserId", "user_id", "userId"))
            if user_id is not None:
                return user_id
    return None
