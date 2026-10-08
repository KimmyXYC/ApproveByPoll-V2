# ApproveByPoll-V2

A Telegram bot that manages group join requests with voting workflows.

## Highlights

- Vote-based join approval with timeout, minimum-voter threshold, and admin override actions.
- Two voting modes:
  - Normal Telegram poll mode.
  - Advanced button mode (Yes/No + live result query).
- Multi-language support (`en_US`, `zh_CN`, `zh_TW`) with per-group language setting.
- Group settings panel with inline controls and `/setting` command arguments.
- Optional log channel updates (Pending -> Approved/Denied edit-in-place).
- PostgreSQL or SQLite storage for group settings and join request lifecycle.

## Requirements

- Python `3.12+`
- PostgreSQL `14+` (recommended 15/16), or SQLite with no separate database server
- Telegram Bot token

## Quick Start (Local)

1. Prepare config files.
2. Install dependencies with `uv` or `pdm`.
3. Start the bot.

```bash
uv sync
uv run python main.py
```

Or with PDM:

```bash
pdm install
pdm run python main.py
```

## Configuration

### 1) Telegram token (`.env`)

Copy `.env.exp` to `.env` and fill your token:

```dotenv
TELEGRAM_BOT_TOKEN=123456:ABC...
# TELEGRAM_BOT_PROXY_ADDRESS=socks5://127.0.0.1:7890
```

### 2) Runtime settings (`conf_dir/.secrets.toml`)

Copy `conf_dir/.secrets.toml.exp` to `conf_dir/.secrets.toml` and edit values:

```toml
[botapi]
enable = false
api_server = "http://127.0.0.1:8081"

[database]
backend = "postgresql"
host = "127.0.0.1"
port = 5432
user = "postgres"
password = "postgres"
dbname = "postgres"

[logchannel]
enable = false
channel_id = -1001234567890
message_thread_id = 0
```

`message_thread_id = 0` means "do not use thread id".

Choose the database with `database.backend`. Existing configurations without this
field continue to use PostgreSQL. To use SQLite, replace the `[database]` section
above with:

```toml
[database]
backend = "sqlite"
path = "data/approvebypoll.sqlite3"
```

SQLite does not require PostgreSQL connection settings. The database file and its
parent directories are created automatically. Relative paths are resolved from
the bot's working directory; the default path is `data/approvebypoll.sqlite3`.
The bot needs write permission to that directory. SQLite timestamps are stored
as UTC text, and writes are committed automatically.

Environment variables `DYNACONF_DATABASE__BACKEND` and `DYNACONF_DATABASE__PATH`
can also select the backend and SQLite path when exported before starting the bot.
Switching backends does not migrate existing data; each database retains its own
group settings and join request history.

### 3) App settings (`conf_dir/settings.toml`)

```toml
[app]
debug = false
```

## Run

```bash
python main.py
```

On startup, the bot connects to the selected database and creates required tables
if missing. `database_setup.sql` is only for manual PostgreSQL setup.

## Commands

- `/help` - Show help information.
- `/setting` - Open group settings panel.
- `/setting time <seconds|10m30s|2h|30d>` - Set vote duration (30 seconds to 30 days, up to `2592000` seconds).
- `/setting voter <count>` - Set minimum voters (`1-500`).
- `/setting mini_voters <count>` - Alias for `voter`.

## Docker

### Build image

```bash
docker build -t approvebypoll-v2:local .
```

### Run with Docker Compose

1. Copy and edit config files:
   - `.env.exp` -> `.env`
   - `conf_dir/.secrets.toml.exp` -> `conf_dir/.secrets.toml`
   - For the bundled PostgreSQL service, set `backend = "postgresql"`,
     `host = "postgres"`, and `user`, `password`, and `dbname` to `"approvebypoll"`.
2. Start:

```bash
docker compose up -d --build
```

3. Logs:

```bash
docker compose logs -f bot
```

4. Stop:

```bash
docker compose down
```

### Run with SQLite (no PostgreSQL service)

Prepare the same config files, then use the standalone SQLite Compose file:

```bash
docker compose -f docker-compose.sqlite.yml up -d --build
docker compose -f docker-compose.sqlite.yml logs -f bot
docker compose -f docker-compose.sqlite.yml down
```

This file selects SQLite through environment variables and persists the database
in the `sqlite_data` volume at `/app/data/approvebypoll.sqlite3`. The volume survives
container recreation and `down`; `down -v` deletes it and its database.
Stop an existing PostgreSQL bot container before switching Compose files.

## Systemd Service

This repo provides a ready-to-use unit file: `approvebypoll.service`.

1. Prepare runtime files first:
   - `.env`
   - `conf_dir/.secrets.toml`
   - `conf_dir/settings.toml`
2. Create a dedicated Linux user (recommended).
3. Put the project at `/opt/ApproveByPoll-V2` (or adjust paths in the unit file).
4. Ensure virtualenv exists at `/opt/ApproveByPoll-V2/.venv`.

Install and enable:

```bash
sudo cp approvebypoll.service /etc/systemd/system/approvebypoll.service
sudo systemctl daemon-reload
sudo systemctl enable --now approvebypoll
```

Manage service:

```bash
sudo systemctl status approvebypoll
sudo systemctl restart approvebypoll
sudo journalctl -u approvebypoll -f
```

If your deployment path or Python path differs, edit `WorkingDirectory` and `ExecStart` in `approvebypoll.service`.

## GitHub Container Registry (GHCR)

This repo includes a workflow to auto-build and push Docker images to GHCR.

- Workflow file: `.github/workflows/docker-ghcr.yml`
- Trigger:
  - Push to `main`
  - Tag pushes `v*`
  - Manual dispatch

Published image path format:

`ghcr.io/<owner>/<repo>:<tag>`

Examples:

- `ghcr.io/kimmyxyc/approvebypoll-v2:main`
- `ghcr.io/kimmyxyc/approvebypoll-v2:latest`
- `ghcr.io/kimmyxyc/approvebypoll-v2:v2.0.0`

## Notes

- Keep `.env` and `conf_dir/.secrets.toml` out of Git.
- For production, give the bot only required admin permissions.
- If poll sending fails in your Telegram environment, the bot can fallback to advanced button voting mode.

## Restart recovery

Both SQLite and PostgreSQL persist ongoing join requests automatically. No new
configuration or external queue is needed. Use a persistent SQLite **file** (not
`:memory:`), or a persistent PostgreSQL database, and run only **one instance per
bot/database**. The recovery database is bound to the Telegram bot ID at startup;
do not reuse it for another bot token's identity.

- Voting keeps its original deadline and the group settings captured when the
  request was created. Restarting does not extend voting time. Both voting modes
  support up to 30 days; the settings menu includes day/week presets, and
  `/setting time 30d` selects the maximum. Existing database constraints are
  upgraded automatically without changing saved settings or active deadlines.
- Advanced votes are committed before the bot acknowledges them. The original
  voter identity, option and name survive a restart; repeat votes are rejected.
  A vote must first reach the durable inbox **before** the deadline. A button click
  first received after the deadline does not count, even if clicked while offline.
- Native polls use Telegram's `close_date` and only confirmed, closed poll totals
  are used for automatic decisions. If an automatically closed poll cannot be
  stopped again, the bot temporarily updates its administrator controls to read
  the full Poll returned in the edited Message, then immediately clears those
  controls. Native polls have no result-query button. Missing final totals never
  count as zero votes. For votes longer than 48 hours, Telegram may refuse to
  delete the original message at cleanup; the poll is still closed and its
  confirmed approval result is retained.
- Approvals, rejections and bans are recorded as successful only after the API
  result is confirmed. Pending decisions, result notifications, unpinning and the
  60-second message cleanup continue after restart.
- If an administrator approves through Telegram, a member-joined update closes
  the vote and synchronizes the database. Membership is also checked before
  settlement. Telegram does not send a dedicated rejection update: when an
  approval/rejection call explicitly reports `HIDE_REQUESTER_MISSING`, membership
  is checked and the request is closed as externally approved or ended with an
  unknown outcome (`waiting = false`, `result = NULL`). It is never reported as a
  successful bot rejection merely because the user is absent. A later, distinct
  join request replaces the stale request atomically. External closure retains
  history, cancels voting and runs the usual recoverable cleanup.
- An explicit deactivated-applicant error during approval, rejection, banning or
  applicant membership checks closes the request with `waiting = false` and
  `result = NULL`, then cleans up the vote. It is displayed as an account
  deactivation, not a successful rejection or a permissions failure. Persisted
  deactivation errors from earlier versions are also handled automatically after
  upgrading and restarting; other permission errors still require review.
- Telegram updates are committed locally before the receiving cursor advances.
  Received updates are replayed in order. Telegram keeps undelivered updates for
  at most 24 hours, so a longer outage can require manual recovery.

### Administrator recovery

Use `/recover` in the affected group to list requests requiring attention and
show **Retry recovery**, **Approve**, **Reject**, and **Ban** buttons. The bot
checks current administrator invite permissions and the originating group for
each action. Applicant status queries distinguish pending, processing, recovery
requiring attention, and confirmed results.

Transient operations retry with exponential delays of 2–60 seconds, respecting
Telegram's longer `retry_after`. Ten consecutive failures require administrator
attention. Explicit retry resets that retry budget. Missing messages and failed
auxiliary notifications do not reverse a confirmed approval/rejection.

Telegram and the database cannot commit one atomic transaction. If a poll/message
may have been sent but its returned ID was not committed, the bot does **not**
blindly send another poll. Likewise, an uncertain rejection is not marked as
successful. Such requests require administrator review. Retry can reconcile
verifiable membership or recover a subsequently received final poll result, but
it cannot reconstruct an unknown message ID. Administrators can explicitly choose
a new approval/rejection/ban decision for requests whose application has not been
confirmed; confirmed results can only resume their remaining cleanup.

### Upgrade and operation

1. Stop the old bot and back up its database before deploying this version. For
   SQLite, stop all writers and use SQLite's backup command; do not copy only the
   main file while WAL writes are active. For PostgreSQL, use `pg_dump`.
2. Start the new version against the same database. Recovery tables and indexes
   are created idempotently, without deleting existing settings/history.
3. Check startup recovery counts and `/recover`. Old waiting requests lack the
   required deadline, message and voter snapshots and therefore need manual
   handling. Completed historical records remain unchanged. Full automatic
   recovery applies to requests created by this version.

Keep the database directory/volume across redeployments. On normal SIGTERM/SIGINT
shutdown, update reception stops first and current work gets up to 10 seconds to
finish before tasks are cancelled and the database is closed. An abrupt kill is
covered by persisted operation states. Database failures stop processing rather
than allowing Telegram actions to proceed without durable records.

Successfully processed inbox bodies are deleted after 7 days; unprocessed/failed
updates are retained. Task snapshots, voter names and operation records remain in
the database alongside request history; protect it like the existing bot logs.
The automated recovery suite uses both real database engines, simulated Telegram
API failures, and subprocess SIGKILL tests. Live Telegram validation additionally
requires a dedicated bot and test group; never run a second receiver beside an
existing deployment.


## Tests

```bash
uv sync --extra dev
uv run pytest -q
uv run ruff check .
```

SQLite tests use temporary database files. To also run the same storage tests
against PostgreSQL, export `TEST_POSTGRES_DSN` (for example,
`postgresql://postgres:password@127.0.0.1:5432/postgres`). The test role needs
permission to create databases; each test creates and removes its own `abp_test_*`
database without modifying existing tables.

## License

MIT
