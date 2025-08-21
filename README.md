# HR Psychometrics Telegram Bot (RU/UZ)

Professional Telegram bot for RU/UZ with three test modules (Ponomarenko, Lichko, Leonhard).
This repo contains:
- `bot.py` — aiogram v3 bot (RU/UZ), results in both languages, HR share, CSV export.
- `surveys.json` — questionnaires (with reverse-scored items).
- `requirements.txt`
- `Railway.toml`
- `.env.example`

## 1) Local run
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=XXXXXXXX
export ADMIN_IDS=123456789
python bot.py
```

## 2) GitHub
Create a repository, add all files (`bot.py`, `surveys.json`, etc.) and push.

## 3) Deploy on Railway (recommended)
1. Go to **Railway** → New Project → **Deploy from GitHub** → select this repo.
2. Railway will auto-detect Python (Nixpacks). Start command is defined in `Railway.toml`.
3. In **Variables**, add:
   - `TELEGRAM_BOT_TOKEN` = your bot token from @BotFather
   - `ADMIN_IDS` = comma-separated Telegram numeric IDs (optional)
   - Optionally `DB_PATH` = `/data/data.sqlite` and add a **Volume** mounted at `/data` to persist the SQLite file.
4. Press **Deploy**. The bot uses long polling and will start automatically.

## 4) Telegram setup
- Create a bot via **@BotFather**, copy token.
- Add your Telegram ID to `ADMIN_IDS` to receive HR shares and allow `/export`.

## 5) How to use
- `/start` → select language (RU/UZ) → choose a test.
- Consent screen → Likert (1–5) questions.
- Results: both RU and UZ summary + Top factors.
- Button **Share HR** sends compact RU result to admins.
- Admin can `/export` a CSV of results.

## Notes
- All tests are self-report screens (not medical diagnosis).
- Reverse-scored items are marked `"rev": true` in `surveys.json`.
- You can extend surveys by adding more items and scales; the bot will auto-pick them.
