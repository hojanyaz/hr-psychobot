
# Modular Surveys Setup

## What to upload to GitHub
- `bot_modular.py` (use this instead of `bot.py`)
- `requirements.txt` (same as before)
- `railway.toml` (start command must be: `python bot_modular.py`)
- `surveys/ponomarenko.v2025-08-21.json`
- `surveys/lichko.v2025-08-21.json`
- `surveys/leonhard.v2025-08-21.json`

## Railway
- Variables:
  - TELEGRAM_BOT_TOKEN = <your token>
  - ADMIN_IDS = 320487406
  - DB_PATH = /data/data.sqlite
  - SURVEY_DIR = surveys
- Volume: mount at `/data` for DB persistence.
- Start command (if not using railway.toml): `python bot_modular.py`

## Use
- /start → choose language → choose test.
- /reload (admin) → reloads JSON files from `surveys/` without redeploy.
