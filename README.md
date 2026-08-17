# shlink-tg-bot

Telegram bot that turns links and files into short URLs via [Shlink](https://shlink.io/).

## Features

- Shorten any link sent in a private chat or via inline mode
- Upload files (documents, photos, videos, audio) and get a short link to download them
- Optional expiry: 1 day / 3 days / 7 days, or permanent (admin only)
- Whitelist-based access control with an `/add` / `/remove` / `/db` admin panel
- Background job that deletes expired files and cleans up dangling short links

## Requirements

- Python 3.10+
- A running [Shlink](https://shlink.io/) instance with an API key
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- (optional) An HTTP server (nginx, etc.) serving `FILES_DIR` so uploaded files are downloadable

## Setup

```bash
pip install aiogram aiohttp

cp .env.example .env
# fill in .env with your bot token, Shlink API key/URL, domain, and your Telegram user id
```

Load the `.env` file however you prefer (e.g. `python-dotenv`, `docker-compose env_file`, or export the variables manually before running).

Run:

```bash
python shlink.py
```

## Configuration

| Variable        | Required | Description                                             |
|-----------------|----------|-----------------------------------------------------------|
| `BOT_TOKEN`     | yes      | Telegram bot token                                       |
| `SHLINK_URL`    | no       | Shlink API base URL (default: `http://localhost:8082`)   |
| `SHLINK_API`    | yes      | Shlink API key                                            |
| `SHLINK_DOMAIN` | yes      | Public domain used for generated short links              |
| `ADMIN_ID`      | yes      | Your numeric Telegram user id (full access, bypasses whitelist) |
| `FILES_DIR`     | no       | Where uploaded files are stored (default: `/var/www/files`) |

## Notes

- `users.json`, `files.json`, and `slugs.json` are created automatically in the working directory to persist the whitelist, uploaded files, and link ownership.
- `MAX_FILE_SIZE` (50 MB by default) applies to non-admin users; edit the constant in `shlink.py` to change it.
