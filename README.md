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
- `/setting time <seconds|10m30s>` - Set vote duration (`30-3600` seconds).
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
