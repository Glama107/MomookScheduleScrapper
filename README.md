# momook-ics

[MOMook](https://momook.com/) — the training-management platform used by flight
schools and ATOs — has no calendar export. This service signs in to your
account, reads your schedule through the app's own REST API, and serves it as
an `.ics` feed your phone can subscribe to. When the school moves a lesson, your
calendar follows.

It uses a private API with your own credentials to read your own data. Nothing
is bypassed, but the API is undocumented and may change without notice.

## How it works

Everything below was found in the public JavaScript bundle served at
`my.momook.com/react-ui` — the official front-end is a React SPA talking to a
PHP backend. These are exactly the calls the app makes.

| Step | Call |
|---|---|
| Sign in | `POST /api/system/auth/session` — `{"loginData": {"username", "password", "auth2fa"}}` → `PHPSESSID` cookie |
| Who am I | `GET /api/system/user/identity` |
| Schedule | `GET /api/schedule?:Start=<ts&:End=>ts&ScheduleEventUser:UserId[]=<id>&Rel[]=…` |

Worth knowing:

- The login form carries **no reCAPTCHA** (it is only used on password reset), so
  programmatic sign-in works.
- Two-factor auth is a two-step exchange: the first POST answers
  `{"status": "auth2fa"}`, the second replays the same payload with an
  `auth2fa` field. MOMook uses standard TOTP (SHA1 / 6 digits / 30 s).
- Bad credentials return `422` with `["page.login.error.invalidCredentials"]`.
- Date filters are unix timestamps prefixed with a comparator: `:Start=<end`
  and `:End=>start`, i.e. the event overlaps the window.
- The `Rel[]` relations are essential. Without them the response carries only
  foreign keys — no room, subject, or instructor names.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp .env.example .env      # then fill it in
```

`MOMOOK_TOTP_SECRET` is the `secret=` parameter of the `otpauth://` URI behind
your 2FA QR code. Leave it empty if 2FA is off.

## Use

```bash
momook-ics whoami          # verify credentials and 2FA
momook-ics events          # your schedule as text
momook-ics ics -o out.ics  # write a calendar file
momook-ics dump -n 3       # raw API JSON, to adjust the mapping
momook-ics serve           # http://localhost:8080/calendar/<token>.ics
```

To run it continuously, the shipped image serves the same feed:

```bash
docker compose up -d --build
```

Then subscribe: on iOS, Settings → Apps → Calendar → Accounts → Add Account →
Other → **Add Subscribed Calendar**, and paste
`https://<your-host>/calendar/<MOMOOK_FEED_TOKEN>.ics`.

The feed carries no alerts: each event ships an `ACTION:NONE` alarm marked
`X-APPLE-DEFAULT-ALARM`, which stops iOS and macOS from applying their default
alert times to a schedule you did not ask to be woken up about. Clients that
ignore that convention still need the manual switch — on iOS, Settings → Apps →
Calendar → Accounts → *the subscribed calendar* → **Remove Alarms**.

`MOMOOK_FEED_TOKEN` is the only thing guarding the feed — a calendar
subscription cannot authenticate any other way — so make it long and random
(`openssl rand -hex 24`). Changing it revokes the old URL instantly.

## Configuration

Every setting is an environment variable prefixed with `MOMOOK_`; see
`.env.example`.

| Variable | Default | Purpose |
|---|---|---|
| `BASE_URL` | `https://my.momook.com` | your MOMook instance |
| `USERNAME` / `PASSWORD` | — | required |
| `TOTP_SECRET` | — | base32 2FA secret, if enabled |
| `FEED_TOKEN` | — | secret path segment of the feed URL |
| `TIMEZONE` | `Europe/Paris` | applied to timestamps returned without an offset |
| `DAYS_PAST` / `DAYS_FUTURE` | 7 / 90 | feed window |
| `CHUNK_DAYS` | 21 | window is fetched in slices; MOMook 504s on wide queries |
| `CACHE_TTL` | 1800 | seconds between background refreshes |
| `HTTP_TIMEOUT` | 120 | MOMook is slow; refreshes run off the request path |
| `ONLY_MY_EVENTS` | `true` | keep only sessions you are enrolled in |
| `HIDE_CANCELLED` | `false` | drop cancellations instead of striking them through |
| `CALENDAR_NAME` | `Momook` | name shown in the calendar app |

If the feed comes out empty, your school may attach lessons to your group rather
than to you individually: set `ONLY_MY_EVENTS=false`. If times are off, check
`TIMEZONE` — `momook-ics dump -n 1` shows the exact format the API returned.
The mapping to `SUMMARY`/`DESCRIPTION` lives in `momook_ics/model.py`.

## Tests

```bash
python -m tests.test_model   # event mapping and ICS output, offline
python -m tests.test_app     # HTTP routes, MOMook stubbed
```

## License

MIT — see [LICENSE](LICENSE).
