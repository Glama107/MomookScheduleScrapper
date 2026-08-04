# momook-ics

[MOMook](https://momook.com/) — the training-management platform used by flight
schools and ATOs — has no calendar export. This service signs in to your
account, reads your schedule through the app's own REST API, and serves it as
an `.ics` feed your phone can subscribe to. When the school moves a lesson, your
calendar follows.

It uses a private API with each subscriber's own credentials to read their own
data. Nothing is bypassed, but the API is undocumented and may change without
notice. One instance can serve [several people](#several-people).

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
momook-ics add             # add somebody, and print the URL to hand them
momook-ics accounts --urls # the roster, with everyone's feed URL
momook-ics url Marie       # one URL, to copy and paste
momook-ics remove Marie    # take somebody off the roster

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

On a host that runs it that way, `bin/momook` is the same CLI against the
deployment — it runs the built image with the directory mounted so the `.env`
can be edited, and restarts the service when the roster changed:

```bash
./bin/momook add            # add somebody, then restart
./bin/momook urls           # everyone's URL
./bin/momook remove Marie
./bin/momook health         # what each feed is doing
```

Then subscribe: on iOS, Settings → Apps → Calendar → Accounts → Add Account →
Other → **Add Subscribed Calendar**, and paste
`https://<your-host>/calendar/<MOMOOK_FEED_TOKEN>.ics`.

## Several people

One instance can serve a handful of colleagues. Each gets a numbered block in
the `.env` and their own feed URL, and `add` writes the block for you:

```console
$ momook-ics add
Name: Marie
Momook username: marie@example.com
Momook password:
2FA secret (blank if 2FA is off):
  signed in as marie@example.com (user 4471)
  23 events in the next 21 days

✓ Marie written to .env as MOMOOK_ACCOUNT_2_*
  previous file kept as .env.bak-20260804-101122

  https://momook.example.com/calendar/6de5a60fc9a0d5b1765a8e3cc47757d6.ics

The service serves it once it restarts.
```

It signs in before writing anything down, so a mistyped password fails there and
then rather than half an hour later as a background refresh; it generates the
feed token; and it picks the block number, which is the part that goes wrong by
hand — a block pasted with the number left as it was replaces the person above
it, and neither dotenv nor the service says a word. `accounts` warns when the
file already has such a pair.

That trial sign-in also settles `ONLY_MY_EVENTS`: if nothing is booked in the
person's name but their group has events, the block is written with
`ONLY_MY_EVENTS=false`, which is otherwise the usual reason a new feed comes out
empty.

```bash
momook-ics accounts --urls     # the roster, and the URL to hand each person
momook-ics url Marie           # just the URL
momook-ics remove Marie        # and off again
momook-ics -a Marie events     # -a takes a label, a block number or a username
```

Set `MOMOOK_PUBLIC_URL` to the address the deployment answers at, and these
print URLs people can subscribe to rather than bare paths.

Writing to the `.env` keeps the previous file as `.env.bak-<timestamp>`, and
narrows the permissions to the owner if they were wider — it is a file full of
plaintext passwords. Blocks can still be written by hand; `add` only spares you
the parts that are easy to get wrong.

Anything a block leaves out falls back to the global value, so
`MOMOOK_TIMEZONE` and friends are set once. `CALENDAR_NAME`, `TIMEZONE`,
`ONLY_MY_EVENTS` and `HIDE_CANCELLED` can be overridden per person.

Two accounts may not share a feed token — the service refuses to start, since
they would serve each other's schedule. Nor should two share a *login*: Momook
ends a session when the same user signs in again, so two feeds on one account
would spend their time invalidating each other. `add` refuses a username that is
already configured for that reason. `serve` is all or nothing: every block must
be complete. One-off commands only validate the account they act on.

Refreshes cost far more than they look: a single one is a series of queries
holding a whole schedule window in memory. So they run **one at a time** on a
single shared thread, in round-robin, `MOMOOK_REFRESH_GAP` seconds apart —
adding people lengthens the cycle instead of multiplying the load on the
school's server. Keep `CACHE_TTL` above roughly *(number of accounts × 2 min)*;
the log warns when the roster stops fitting in the cycle.

Requests are only ever answered from the cache, never by fetching: a feed whose
first refresh has not landed yet returns `503` rather than holding the
connection open for minutes. `/healthz` reports every account's cache age and
last error, and contains no tokens.

**What this does not do:** the passwords sit in the `.env` in plain text, and
the service needs them that way — it re-authenticates unattended every
`CACHE_TTL`, so nothing one-way like a hash can stand in for them. Whoever
administers the host can read them. Hosting other people's credentials is a
promise; make sure they know they are making it.

The feed carries no alerts: each event ships an `ACTION:NONE` alarm marked
`X-APPLE-DEFAULT-ALARM`, which stops iOS and macOS from applying their default
alert times to a schedule you did not ask to be woken up about.

Leave the subscription's **Remove Alarms** switch *off*. It strips every VALARM
the feed sends — including the silent one — which puts the event back in the
"no alarm" state that invites the default alert. The two settings work against
each other; the feed already says what the switch was meant to say.

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
| `PUBLIC_URL` | — | where this deployment answers, so the CLI can print a URL |
| `TIMEZONE` | `Europe/Paris` | applied to timestamps returned without an offset |
| `DAYS_PAST` / `DAYS_FUTURE` | 7 / 90 | feed window |
| `CHUNK_DAYS` | 21 | window is fetched in slices; MOMook 504s on wide queries |
| `CACHE_TTL` | 1800 | seconds between two refreshes of the same account |
| `REFRESH_GAP` | 15 | seconds of quiet between two accounts' refreshes |
| `HTTP_TIMEOUT` | 120 | MOMook is slow; refreshes run off the request path |
| `ONLY_MY_EVENTS` | `true` | keep only sessions you are enrolled in |
| `HIDE_CANCELLED` | `false` | drop cancellations instead of striking them through |
| `CALENDAR_NAME` | `Momook` | name shown in the calendar app |

Add a person with `MOMOOK_ACCOUNT_<n>_…`: `LABEL`, `USERNAME`, `PASSWORD`,
`TOTP_SECRET`, `FEED_TOKEN`, plus `CALENDAR_NAME`, `TIMEZONE`,
`ONLY_MY_EVENTS` and `HIDE_CANCELLED` to override a global. A misspelt name is
refused at start-up rather than silently ignored.

If the feed comes out empty, your school may attach lessons to your group rather
than to you individually: set `ONLY_MY_EVENTS=false`. If times are off, check
`TIMEZONE` — `momook-ics dump -n 1` shows the exact format the API returned.
The mapping to `SUMMARY`/`DESCRIPTION` lives in `momook_ics/model.py`.

## Tests

```bash
python -m tests.test_model    # event mapping and ICS output, offline
python -m tests.test_config   # account blocks, inheritance, token rules
python -m tests.test_envfile  # writing the .env: quoting, numbering, removal
python -m tests.test_client   # session recovery, MOMook stubbed
python -m tests.test_app      # HTTP routes, MOMook stubbed
```

## License

MIT — see [LICENSE](LICENSE).
