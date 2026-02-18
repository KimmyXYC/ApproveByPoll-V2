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
- PostgreSQL storage for group settings and join request lifecycle.

## Requirements

- Python `3.12+`
- PostgreSQL `14+` (recommended 15/16)
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

### 3) App settings (`conf_dir/settings.toml`)

```toml
[app]
debug = false
```

## Run

```bash
python main.py
```

On startup, the bot connects to PostgreSQL and creates required tables if missing.

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

## License

MIT
