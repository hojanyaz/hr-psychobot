import os
import asyncio
import json
import io
import csv
import glob
import random
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputFile,
)

# For radar chart generation
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

# === Environment variables ===
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = set(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)
DB_PATH = os.getenv("DB_PATH", "data.sqlite")
SURVEY_DIR = os.getenv("SURVEY_DIR", "surveys")

# Validity thresholds
MIN_SEC_PER_ITEM = float(os.getenv("MIN_SEC_PER_ITEM", "1.5"))
STRAIGHT_LINING_VAR = float(os.getenv("STRAIGHT_LINING_VAR", "0.2"))

# Google Sheets integration (optional)
SHEETS_DOC_ID = os.getenv("SHEETS_DOC_ID")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

if not TOKEN:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN")

# === Bot and dispatcher ===
bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

# === Global data structures ===
SURVEYS: Dict[str, dict] = {}
INTERP: Dict[str, dict] = {}
ROLE_TIPS: Dict[str, dict] = {}

# In-memory user language and sessions
user_lang: Dict[int, str] = {}
pending_role: Dict[int, str] = {}

@dataclass
class Session:
    user_id: int
    survey_key: str
    lang: str = "ru"
    idx: int = 0
    answers: List[int] = field(default_factory=list)
    order: List[int] = field(default_factory=list)
    started_at: float = 0.0

# === Loading surveys and configs ===
def load_surveys(dir_path: Optional[str] = None) -> None:
    """Load surveys from JSON files in the specified directory."""
    global SURVEYS
    SURVEYS.clear()
    path = dir_path or SURVEY_DIR
    if not os.path.isdir(path):
        return
    for fname in glob.glob(os.path.join(path, "*.json")):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                s = json.load(f)
            # Each survey file should contain a key and other fields
            key = s.get("key") or os.path.splitext(os.path.basename(fname))[0]
            status = s.get("status", "active")
            if status != "active":
                continue
            SURVEYS[key] = s
        except Exception:
            continue

def load_config() -> None:
    """Load interpretations and roles tips from config files."""
    global INTERP, ROLE_TIPS
    # Interpretations
    try:
        with open(os.path.join("config", "interpretations.json"), "r", encoding="utf-8") as f:
            INTERP = json.load(f)
    except Exception:
        INTERP = {}
    # Roles tips
    try:
        with open(os.path.join("config", "roles_tips.json"), "r", encoding="utf-8") as f:
            ROLE_TIPS = json.load(f)
    except Exception:
        ROLE_TIPS = {}

# Load at startup
load_surveys(SURVEY_DIR)
load_config()

# === Localization texts ===
LOCALES = ("ru", "uz")
ROLE_OPTIONS = {
    "ru": ["Sales", "Logistics", "Finance", "R&D", "HR", "Manager", "Пропустить"],
    "uz": ["Sales", "Logistics", "Finance", "R&D", "HR", "Manager", "O‘tkazish"],
}
ROLE_PROMPT = {
    "ru": "Пожалуйста, выберите вашу роль или пропустите:",
    "uz": "Iltimos, rolingizni tanlang yoki o'tkazib yuboring:",
}

START_TEXT = {
    "ru": "Привет! Выберите язык:\n\nРусский / O‘zbekcha",
    "uz": "Salom! Tilni tanlang:\n\nРусский / O‘zbekcha",
}

CONSENT = {
    "ru": (
        "⚖️ Дисклеймер\n\n"
        "• Это самооценочные опросники (не медицинская диагностика).\n"
        "• Данные сохраняются для HR-отчётов при вашем согласии (кнопка «Поделиться HR»).\n"
        "• Можно пройти анонимно и не делиться результатом.\n\n"
        "Нажмите «Согласен», чтобы начать."
    ),
    "uz": (
        "⚖️ Ogohlantirish\n\n"
        "• Bu o'zini baholash testlari (tibbiy diagnostika emas).\n"
        "• Ma'lumotlar HR hisobotlari uchun sizning roziligingiz bilan saqlanadi (\"HRga ulashish\" tugmasi).\n"
        "• Siz anonim qolishingiz va natijani ulashmasligingiz mumkin.\n\n"
        "Boshlash uchun «Roziman» tugmasini bosing."
    ),
}

SCALE_TEXT = {
    "ru": "Оцените по шкале 1–5, где 1 = совсем не про меня, 5 = полностью про меня.",
    "uz": "1–5 shkala bo'yicha baholang, 1 = men uchun mutlaqo to'g'ri emas, 5 = men uchun to'liq to'g'ri."
}

AGREE_BUTTON = {"ru": "Согласен", "uz": "Roziman"}
BACK_BUTTON = {"ru": "Назад", "uz": "Orqaga"}
HOME_BUTTON = {"ru": "В меню", "uz": "Menyu"}
SHARE_BUTTON = {"ru": "📤 Поделиться HR", "uz": "📤 HRga ulashish"}
PREVIEW_BUTTON = {"ru": "📄 Просмотр отчёта", "uz": "📄 Hisobot ko'rish"}

HELP_TEXT = {
    "ru": (
        "ℹ️ Помощь\n\n"
        "/start — начать заново\n"
        "/help — эта справка\n"
        "/reload — перезагрузить опросы (только для админов)\n"
        "/export — скачать CSV результатов (только для админов)\n"
        "/team <название> — установить команду для пользователя (реплай) или себя (только для админов)\n"
        "/stats — статистика (только для админов)\n"
        "Ваши данные используются только с согласия и хранятся ограниченное время."
    ),
    "uz": (
        "ℹ️ Yordam\n\n"
        "/start — qayta boshlash\n"
        "/help — bu ma'lumot\n"
        "/reload — testlarni qayta yuklash (faqat adminlar uchun)\n"
        "/export — CSV natijalarni yuklab olish (faqat adminlar uchun)\n"
        "/team <nom> — foydalanuvchi uchun jamoani o'rnatish (reply) yoki o'zingiz uchun (faqat adminlar uchun)\n"
        "/stats — statistika (faqat adminlar uchun)\n"
        "Ma'lumotlaringiz faqat sizning roziligingiz bilan foydalaniladi va cheklangan muddat saqlanadi."
    ),
}

# === Keyboard builders ===
def lang_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Русский"), KeyboardButton(text="O‘zbekcha")]],
        resize_keyboard=True,
    )

def menu_kb(lang: str):
    rows = []
    for key, s in SURVEYS.items():
        rows.append([KeyboardButton(text=s["title"][lang])])
    # About/help button optional
    rows.append([KeyboardButton(text="ℹ️ О боте")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def likert_kb(lang: str):
    row = [InlineKeyboardButton(text=str(i), callback_data=f"ans:{i}") for i in range(1, 6)]
    row2 = [InlineKeyboardButton(text=BACK_BUTTON[lang], callback_data="back")]
    return InlineKeyboardMarkup(inline_keyboard=[row, row2])

def role_kb(lang: str):
    # inline keyboard for role selection
    buttons = [[InlineKeyboardButton(text=role, callback_data=f"role:{role}")]
               for role in ROLE_OPTIONS[lang]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def continue_kb(lang: str, skey: str):
    # Buttons to continue or restart
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="▶️ Продолжить" if lang == "ru" else "▶️ Davom ettirish", callback_data=f"continue:{skey}")],
            [InlineKeyboardButton(text="🔄 Начать заново" if lang == "ru" else "🔄 Qaytadan boshlash", callback_data=f"restart:{skey}")],
        ]
    )

def preview_kb(lang: str):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить HR" if lang == "ru" else "✅ HRga yuborish", callback_data="send_hr")],
            [InlineKeyboardButton(text="✖️ Отмена" if lang == "ru" else "✖️ Bekor qilish", callback_data="cancel_hr")],
        ]
    )

# === Database helpers ===
async def ensure_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # results table: validity JSON added
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS results(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER,
              lang TEXT,
              survey_key TEXT,
              survey_version TEXT,
              ts DATETIME DEFAULT CURRENT_TIMESTAMP,
              raw JSON,
              scores JSON,
              validity JSON,
              shared INTEGER DEFAULT 0
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users(
              user_id INTEGER PRIMARY KEY,
              lang TEXT,
              role TEXT,
              team TEXT
            );
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS progress(
              user_id INTEGER PRIMARY KEY,
              survey_key TEXT,
              idx INTEGER,
              answers JSON,
              order_json JSON,
              lang TEXT,
              started_at REAL
            );
            """
        )
        await db.commit()

async def save_progress(sess: Session) -> None:
    """Persist ongoing test progress."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO progress(user_id, survey_key, idx, answers, order_json, lang, started_at) VALUES(?,?,?,?,?,?,?)",
            (
                sess.user_id,
                sess.survey_key,
                sess.idx,
                json.dumps(sess.answers, ensure_ascii=False),
                json.dumps(sess.order, ensure_ascii=False),
                sess.lang,
                sess.started_at,
            ),
        )
        await db.commit()

async def clear_progress(uid: int) -> None:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM progress WHERE user_id=?", (uid,))
        await db.commit()

async def get_progress(uid: int) -> Optional[Session]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT survey_key, idx, answers, order_json, lang, started_at FROM progress WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    if not row:
        return None
    survey_key, idx, answers_json, order_json, lang, started_at = row
    answers = json.loads(answers_json)
    order = json.loads(order_json)
    sess = Session(user_id=uid, survey_key=survey_key, lang=lang, idx=idx, answers=answers, order=order, started_at=started_at)
    return sess

async def save_result(uid: int, lang: str, skey: str, ordered_answers: List[int], scores: dict, validity: dict) -> None:
    sdef = SURVEYS[skey]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO users(user_id, lang, role, team) VALUES(?,?, (SELECT role FROM users WHERE user_id=?), (SELECT team FROM users WHERE user_id=?))",
            (uid, lang, uid, uid),
        )
        await db.execute(
            "INSERT INTO results(user_id, lang, survey_key, survey_version, raw, scores, validity) VALUES(?,?,?,?,?,?,?)",
            (
                uid,
                lang,
                skey,
                sdef.get("version", "unknown"),
                json.dumps({"answers": ordered_answers}, ensure_ascii=False),
                json.dumps(scores, ensure_ascii=False),
                json.dumps(validity, ensure_ascii=False),
            ),
        )
        await db.commit()

    # Optional Google Sheets export
    if SHEETS_DOC_ID and GOOGLE_CREDENTIALS_JSON:
        try:
            import gspread
            from oauth2client.service_account import ServiceAccountCredentials
            creds_dict = json.loads(GOOGLE_CREDENTIALS_JSON)
            scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
            creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
            gc = gspread.authorize(creds)
            sh = gc.open_by_key(SHEETS_DOC_ID)
            worksheet = sh.sheet1
            # Prepare row
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(
                    "SELECT role, team FROM users WHERE user_id=?",
                    (uid,),
                )
                urow = await cur.fetchone()
            role, team = (urow or (None, None))
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            worksheet.append_row(
                [ts, str(uid), team or "", role or "", skey, sdef.get("version", "unknown"), lang, json.dumps(scores, ensure_ascii=False), json.dumps(validity, ensure_ascii=False)]
            )
        except Exception:
            pass

# === Survey scoring and validity ===
def reorder_answers(sdef: dict, answers: List[int], order: List[int]) -> List[int]:
    """Reorder shuffled answers back to the original order."""
    total = len(sdef["items"])
    ordered = [None] * total
    for i, ans in enumerate(answers):
        orig_index = order[i]
        ordered[orig_index] = ans
    # Fill any missing entries with 0
    for i in range(total):
        if ordered[i] is None:
            ordered[i] = 0
    return ordered

def score_survey(skey: str, ordered_answers: List[int]) -> (dict, List[tuple]):
    sdef = SURVEYS[skey]
    buckets = {}
    counts = {}
    for ans, item in zip(ordered_answers, sdef["items"]):
        val = 6 - ans if item.get("rev") else ans
        k = item["k"]
        if k == "trap":
            continue
        buckets[k] = buckets.get(k, 0) + val
        counts[k] = counts.get(k, 0) + 1
    for k in buckets:
        buckets[k] = round(buckets[k] / counts[k], 2)
    top = sorted(buckets.items(), key=lambda x: x[1], reverse=True)[:3]
    return buckets, top

def compute_validity(session: Session, ordered_answers: List[int], sdef: dict) -> dict:
    total_items = len(sdef["items"])
    # trap hits: items with k == "trap" and answer >=4
    trap_hits = 0
    for ans, item in zip(session.answers, [sdef["items"][i] for i in session.order]):
        if item.get("k") == "trap" and ans >= 4:
            trap_hits += 1
    elapsed = time.time() - session.started_at if session.started_at else 0
    too_fast = elapsed < total_items * MIN_SEC_PER_ITEM
    # Straight-lining: compute variance of answers
    if len(session.answers) > 1:
        mean = sum(session.answers) / len(session.answers)
        var = sum((a - mean) ** 2 for a in session.answers) / len(session.answers)
    else:
        var = 0
    straight = var < STRAIGHT_LINING_VAR
    return {"trap": trap_hits > 0, "too_fast": too_fast, "straight": straight, "duration": round(elapsed, 2)}

def create_radar_chart(scores: dict, labels: dict) -> str:
    """Create a radar chart image and return file path."""
    categories = list(scores.keys())
    values = [scores[k] for k in categories]
    # repeat first value to close the loop
    values += values[:1]
    N = len(categories)
    angles = [n / float(N) * 2 * np.pi for n in range(N)]
    angles += angles[:1]
    fig = plt.figure(figsize=(4, 4))
    ax = plt.subplot(111, polar=True)
    ax.plot(angles, values, linewidth=1)
    ax.fill(angles, values, alpha=0.1)
    ax.set_xticks(angles[:-1])
    # Use Russian labels for axis ticks
    xtick_labels = [labels[k]["ru"] if k in labels else k for k in categories]
    ax.set_xticklabels(xtick_labels, fontsize=8)
    ax.set_yticks([1, 2, 3, 4, 5])
    ax.set_ylim(0, 5)
    ax.set_title("Профиль (1–5)", fontsize=10)
    temp_path = f"/tmp/profile_{int(time.time()*1000)}.png"
    fig.tight_layout()
    fig.savefig(temp_path, bbox_inches='tight')
    plt.close(fig)
    return temp_path

# === Result rendering ===
def build_result_text(skey: str, scores: dict, top: List[tuple], lang: str, uid: int) -> str:
    sdef = SURVEYS[skey]
    labels = sdef["scoring"]
    lines_ru = [f"📊 *{sdef['title']['ru']}*"] + [f"• *{labels[k]['ru']}*: {v}/5" for k, v in scores.items()]
    lines_uz = [f"📊 *{sdef['title']['uz']}*"] + [f"• *{labels[k]['uz']}*: {v}/5" for k, v in scores.items()]
    # Interpretations
    interp_key = skey
    interp = INTERP.get(interp_key, {})
    # Role overlay
    role_text_ru = []
    role_text_uz = []
    # fetch user role from DB
    # We'll query asynchronously later; placeholder here (role retrieval done elsewhere)
    return "\n".join(lines_ru + lines_uz)


async def build_full_summary(uid: int, skey: str, scores: dict, top: List[tuple], validity: dict) -> str:
    """Compose full bilingual summary with interpretations and validity."""
    sdef = SURVEYS[skey]
    labels = sdef["scoring"]
    # fetch role
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    role = row[0] if row else None
    lines_ru = [f"📊 *{sdef['title']['ru']}*"]
    for k, v in scores.items():
        lines_ru.append(f"• *{labels[k]['ru']}*: {v}/5")
    lines_uz = [f"📊 *{sdef['title']['uz']}*"]
    for k, v in scores.items():
        lines_uz.append(f"• *{labels[k]['uz']}*: {v}/5")
    # Interpretations for top 3
    interp_key = skey
    interp = INTERP.get(interp_key, {})
    tips_ru = []
    tips_uz = []
    for k, val in top:
        trait = k
        trait_interp = interp.get(trait, {})
        ru_block = trait_interp.get("ru", {})
        uz_block = trait_interp.get("uz", {})
        if ru_block:
            tips_ru.append(f"\n*{labels[trait]['ru']}*\n- Сильные: {ru_block.get('strengths','')}\n- Риски: {ru_block.get('risks','')}\n- Советы: {ru_block.get('tips','')}")
        if uz_block:
            tips_uz.append(f"\n*{labels[trait]['uz']}*\n- Kuchli: {uz_block.get('strengths','')}\n- Xavf: {uz_block.get('risks','')}\n- Maslahatlar: {uz_block.get('tips','')}")
        # Role overlays
        if role and ROLE_TIPS:
            role_info = ROLE_TIPS.get(interp_key, {}).get(trait, {}).get(role, {})
            if role_info:
                ru_role = role_info.get("ru")
                uz_role = role_info.get("uz")
                if ru_role:
                    tips_ru.append(f"  • {role}: {ru_role}")
                if uz_role:
                    tips_uz.append(f"  • {role}: {uz_role}")
    # Validity line
    validity_line_ru = []
    validity_line_uz = []
    if validity.get("trap") or validity.get("too_fast") or validity.get("straight"):
        parts_ru = []
        parts_uz = []
        if validity.get("trap"):
            parts_ru.append("внимание: ответы на контрольные вопросы")
            parts_uz.append("diqqat: nazorat savollariga javoblar")
        if validity.get("too_fast"):
            parts_ru.append("слишком быстрое прохождение")
            parts_uz.append("juda tez yakunlangan")
        if validity.get("straight"):
            parts_ru.append("однообразные ответы")
            parts_uz.append("bir xil javoblar")
        validity_line_ru = ["⚠️ Валидация: " + ", ".join(parts_ru)]
        validity_line_uz = ["⚠️ Tekshiruv: " + ", ".join(parts_uz)]
    # Compose summary
    text_ru = "\n".join(lines_ru + validity_line_ru + tips_ru)
    text_uz = "\n".join(lines_uz + validity_line_uz + tips_uz)
    return text_ru + "\n\n" + text_uz

# === Role helper ===
async def ask_role_if_needed(uid: int, lang: str, chat_id: int) -> bool:
    """Ask user to select role if not set. Returns True if role prompt sent."""
    # check DB
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    role = row[0] if row else None
    if not role:
        pending_role[uid] = lang
        await bot.send_message(chat_id, ROLE_PROMPT[lang], reply_markup=role_kb(lang))
        return True
    return False

# === Command handlers ===
@dp.message(Command("start"))
async def cmd_start(m: Message):
    await ensure_db()
    user_lang[m.from_user.id] = "ru"
    await m.answer(START_TEXT["ru"], reply_markup=lang_kb())

@dp.message(Command("help"))
async def cmd_help(m: Message):
    lang = user_lang.get(m.from_user.id, "ru")
    await m.answer(HELP_TEXT[lang])

@dp.message(Command("reload"))
async def cmd_reload(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    load_surveys(SURVEY_DIR)
    load_config()
    await m.answer("Опросы и конфиги перезагружены.")

@dp.message(Command("export"))
async def cmd_export(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ts, user_id, lang, survey_key, survey_version, raw, scores, validity, shared FROM results ORDER BY ts DESC"
        )
        rows = await cur.fetchall()
        # fetch role and team
        # Build CSV
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["ts", "user_id", "lang", "survey", "version", "raw", "scores", "validity", "shared"])
    for r in rows:
        writer.writerow(r)
    buf.seek(0)
    await m.answer_document(FSInputFile.from_file(buf, filename="results.csv"), caption="Экспорт результатов")

@dp.message(Command("team"))
async def cmd_team(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.reply("Укажите название команды")
        return
    team_name = parts[1].strip()
    target_id = None
    if m.reply_to_message:
        target_id = m.reply_to_message.from_user.id
    else:
        target_id = m.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        # update or insert
        await db.execute(
            "INSERT INTO users(user_id, team) VALUES(?, ?) ON CONFLICT(user_id) DO UPDATE SET team=excluded.team",
            (target_id, team_name),
        )
        await db.commit()
    await m.reply("Команда установлена." if user_lang.get(m.from_user.id, "ru") == "ru" else "Jamoa o'rnatildi.")

@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        # last 30 days
        cur = await db.execute(
            "SELECT survey_key, COUNT(*), AVG(CAST(json_extract(validity, '$.duration') AS REAL)), SUM(shared), SUM(CASE WHEN json_extract(validity, '$.trap') OR json_extract(validity, '$.too_fast') OR json_extract(validity, '$.straight') THEN 1 ELSE 0 END) FROM results WHERE ts >= datetime('now','-30 day') GROUP BY survey_key"
        )
        rows = await cur.fetchall()
    if not rows:
        await m.reply("Нет данных")
        return
    lines = []
    for row in rows:
        skey, count, avg_dur, shared_cnt, invalid_cnt = row
        lines.append(
            f"{skey}: {count} прохождений, средняя длительность {round(avg_dur or 0,2)} сек, поделились HR {shared_cnt}/{count}, недостоверных {invalid_cnt}/{count}"
        )
    await m.reply("\n".join(lines))

# === Language selection ===
@dp.message(F.text == "Русский")
async def choose_ru(m: Message):
    user_lang[m.from_user.id] = "ru"
    sent = await ask_role_if_needed(m.from_user.id, "ru", m.chat.id)
    if not sent:
        await m.answer("Выберите тест:", reply_markup=menu_kb("ru"))

@dp.message(F.text == "O‘zbekcha")
async def choose_uz(m: Message):
    user_lang[m.from_user.id] = "uz"
    sent = await ask_role_if_needed(m.from_user.id, "uz", m.chat.id)
    if not sent:
        await m.answer("Testni tanlang:", reply_markup=menu_kb("uz"))

# === Role callback ===
@dp.callback_query(F.data.startswith("role:"))
async def set_role_cb(c: CallbackQuery):
    value = c.data.split(":", 1)[1]
    lang = pending_role.pop(c.from_user.id, user_lang.get(c.from_user.id, "ru"))
    role = None
    # determine skip keywords
    if value not in ("Пропустить", "O‘tkazish"):
        role = value
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO users(user_id, lang, role) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role, lang=excluded.lang",
            (c.from_user.id, lang, role),
        )
        await db.commit()
    await c.message.delete()
    await c.answer()
    await c.message.answer("Выберите тест:" if lang == "ru" else "Testni tanlang:", reply_markup=menu_kb(lang))

# === Pick survey ===
@dp.message(F.text.in_([*(s["title"]["ru"] for s in SURVEYS.values()), *(s["title"]["uz"] for s in SURVEYS.values())]))
async def pick_survey(m: Message):
    lang = user_lang.get(m.from_user.id, "ru")
    # find survey key by title
    skey = None
    for key, s in SURVEYS.items():
        if m.text == s["title"][lang]:
            skey = key
            break
    if not skey:
        return
    # Check for unfinished progress
    prog = await get_progress(m.from_user.id)
    if prog and prog.survey_key == skey:
        # Ask to continue or restart
        await m.answer(
            "У вас уже есть незавершённый тест. Вы хотите продолжить?" if lang == "ru" else "Sizda tugallanmagan test bor. Davom ettirishni xohlaysizmi?",
            reply_markup=continue_kb(lang, skey),
        )
        return
    # New session
    sessions[m.from_user.id] = Session(user_id=m.from_user.id, survey_key=skey, lang=lang)
    await m.answer(CONSENT[lang], reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=AGREE_BUTTON[lang], callback_data="agree")], [InlineKeyboardButton(text=BACK_BUTTON[lang], callback_data="home")]]
    ))

@dp.callback_query(F.data.startswith("continue:"))
async def cb_continue(c: CallbackQuery):
    skey = c.data.split(":", 1)[1]
    lang = user_lang.get(c.from_user.id, "ru")
    prog = await get_progress(c.from_user.id)
    if not prog or prog.survey_key != skey:
        await c.answer("Нет сохранённого теста" if lang == "ru" else "Saqlangan test topilmadi", show_alert=True)
        return
    sessions[c.from_user.id] = prog
    await c.message.delete()
    await c.answer()
    # send scale text only if at beginning
    if prog.idx == 0:
        await bot.send_message(c.message.chat.id, SCALE_TEXT[lang])
    await ask_next(c.message.chat.id, c.from_user.id)

@dp.callback_query(F.data.startswith("restart:"))
async def cb_restart(c: CallbackQuery):
    skey = c.data.split(":", 1)[1]
    lang = user_lang.get(c.from_user.id, "ru")
    await clear_progress(c.from_user.id)
    sessions[c.from_user.id] = Session(user_id=c.from_user.id, survey_key=skey, lang=lang)
    await c.message.delete()
    await c.answer()
    await bot.send_message(c.message.chat.id, CONSENT[lang], reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=AGREE_BUTTON[lang], callback_data="agree")], [InlineKeyboardButton(text=BACK_BUTTON[lang], callback_data="home")]]
    ))

# === Agree callback ===
@dp.callback_query(F.data == "agree")
async def cb_agree(c: CallbackQuery):
    sess = sessions.get(c.from_user.id)
    if not sess:
        await c.answer("No session", show_alert=True)
        return
    # initialize session
    sdef = SURVEYS[sess.survey_key]
    sess.idx = 0
    sess.answers = []
    sess.order = list(range(len(sdef["items"])))
    random.shuffle(sess.order)
    sess.started_at = time.time()
    # Save initial progress
    await save_progress(sess)
    # Send scale info
    await c.message.answer(SCALE_TEXT[sess.lang])
    await c.answer()
    await ask_next(c.message.chat.id, c.from_user.id)

# === Asking next question ===
async def ask_next(chat_id: int, uid: int):
    sess = sessions[uid]
    sdef = SURVEYS[sess.survey_key]
    lang = sess.lang
    if sess.idx >= len(sess.order):
        # Finish test
        ordered_answers = reorder_answers(sdef, sess.answers, sess.order)
        scores, top = score_survey(sess.survey_key, ordered_answers)
        # Compute validity
        validity = compute_validity(sess, ordered_answers, sdef)
        # Save result to DB
        await save_result(uid, lang, sess.survey_key, ordered_answers, scores, validity)
        # Remove progress
        await clear_progress(uid)
        # Build summary text
        summary = await build_full_summary(uid, sess.survey_key, scores, top, validity)
        # Create radar chart
        chart_path = create_radar_chart(scores, sdef["scoring"])
        # Send summary with chart
        with open(chart_path, "rb") as f:
            img_file = InputFile(f, filename="profile.png")
            await bot.send_photo(chat_id, photo=img_file, caption=summary)
        # Preview for HR
        # Build compact RU summary
        labels = sdef["scoring"]
        compact = "\n".join([f"• {labels[k]['ru']}: {v}/5" for k, v in scores.items()])
        await bot.send_message(chat_id, "Предварительный отчёт для HR:\n" + compact, reply_markup=preview_kb(lang))
        sessions.pop(uid, None)
        return
    # Ask next item
    orig_index = sess.order[sess.idx]
    item = sdef["items"][orig_index]
    q = f"{sess.idx+1}/{len(sess.order)}. {item['t'][lang]}"
    await bot.send_message(chat_id, q, reply_markup=likert_kb(lang))

# === Answer callback ===
@dp.callback_query(F.data.startswith("ans:"))
async def cb_answer(c: CallbackQuery):
    uid = c.from_user.id
    sess = sessions.get(uid)
    if not sess:
        await c.answer("Session not found", show_alert=True)
        return
    # Append answer
    val = int(c.data.split(":", 1)[1])
    sess.answers.append(val)
    sess.idx += 1
    # Save progress
    await save_progress(sess)
    await c.answer()
    await ask_next(c.message.chat.id, uid)

@dp.callback_query(F.data == "back")
async def cb_back(c: CallbackQuery):
    sess = sessions.get(c.from_user.id)
    if not sess or sess.idx == 0:
        await c.answer("–")
        return
    sess.idx -= 1
    if sess.answers:
        sess.answers.pop()
    await save_progress(sess)
    await c.answer()
    await ask_next(c.message.chat.id, c.from_user.id)

# === HR preview and send ===
@dp.callback_query(F.data == "send_hr")
async def cb_send_hr(c: CallbackQuery):
    uid = c.from_user.id
    lang = user_lang.get(uid, "ru")
    # fetch last result
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT ts, survey_key, survey_version, scores FROM results WHERE user_id=? ORDER BY ts DESC LIMIT 1",
            (uid,),
        )
        row = await cur.fetchone()
    if not row:
        await c.answer("Нет данных" if lang == "ru" else "Ma'lumot topilmadi", show_alert=True)
        return
    ts, skey, ver, scores_json = row
    sdef = SURVEYS.get(skey) or {}
    labels = sdef.get("scoring", {})
    scores = json.loads(scores_json)
    txt = "👤 @" + (c.from_user.username or str(c.from_user.id)) + "\n" + sdef.get("title", {}).get("ru", "Опрос") + f" v{ver}\n" + "\n".join(f"• {labels.get(k, {'ru': k})['ru'] if k in labels else k}: {v}/5" for k, v in scores.items())
    # update shared flag
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE results SET shared=1 WHERE user_id=? AND ts=?", (uid, ts))
        await db.commit()
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, txt)
        except:
            pass
    await c.answer("Отправлено HR." if lang == "ru" else "HRga yuborildi.", show_alert=True)
    await c.message.delete()

@dp.callback_query(F.data == "cancel_hr")
async def cb_cancel_hr(c: CallbackQuery):
    await c.answer("Отмена" if user_lang.get(c.from_user.id, "ru") == "ru" else "Bekor qilindi", show_alert=True)
    await c.message.delete()

# === Home/back ===
@dp.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery):
    lang = user_lang.get(c.from_user.id, "ru")
    await c.message.answer("Выберите тест:" if lang == "ru" else "Testni tanlang:", reply_markup=menu_kb(lang))
    await c.answer()

@dp.message(F.text == "ℹ️ О боте")
async def about(m: Message):
    lang = user_lang.get(m.from_user.id, "ru")
    await m.answer(HELP_TEXT[lang])

async def main():
    await ensure_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())