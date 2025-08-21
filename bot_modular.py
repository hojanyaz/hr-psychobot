
import os, asyncio, json, io, csv, aiosqlite, glob
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (Message, CallbackQuery, ReplyKeyboardMarkup, KeyboardButton,
                           InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit())
DB_PATH = os.getenv("DB_PATH", "data.sqlite")
SURVEY_DIR = os.getenv("SURVEY_DIR", "surveys")

if not TOKEN:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

# == locales ==
LOCALES = ("ru","uz")
CONSENT = {
 "ru": ("⚖️ Дисклеймер\n\n"
        "• Это самооценочные опросники (не медицинская диагностика).\n"
        "• Данные сохраняются для HR-отчётов при вашем согласии (кнопка «Поделиться HR»).\n"
        "• Можно пройти анонимно и не делиться результатом.\n\n"
        "Нажмите «Согласен», чтобы начать."),
 "uz": ("⚖️ Ogohlantirish\n\n"
        "• Bu o‘z-o‘zini baholash so‘rovlari (tibbiy tashxis emas).\n"
        "• Ma'lumotlar faqat siz rozilik bildirganingizda HR uchun saqlanadi.\n"
        "• Anonim o‘tish va natijani ulashmaslik mumkin.\n\n"
        "Boshlash uchun «Roziman» tugmasini bosing.")
}
AGREE_BUTTON = {"ru":"✅ Согласен","uz":"✅ Roziman"}
BACK_BUTTON = {"ru":"🔙 Назад","uz":"🔙 Orqaga"}
SHARE_BUTTON = {"ru":"📤 Поделиться HR","uz":"📤 HR bilan ulashish"}
HOME_BUTTON = {"ru":"🏠 В меню","uz":"🏠 Menyuga"}
ABOUT_BUTTON = {"ru":"ℹ️ О боте","uz":"ℹ️ Bot haqida"}
START_TEXT = {"ru":"Выберите язык / Tilni tanlang:","uz":"Tilni tanlang / Выберите язык:"}
MENU_TEXT = {"ru":"Выберите тест:","uz":"Testni tanlang:"}
SCALE_TEXT = {"ru":"Шкала: 1 (совсем не про меня) … 5 (полностью про меня)","uz":"Shkala: 1 (mutlaqo to‘g‘ri emas) … 5 (to‘liq to‘g‘ri)"}

# == surveys loader ==
SURVEYS: Dict[str, dict] = {}
def load_surveys():
    SURVEYS.clear()
    for path in glob.glob(os.path.join(SURVEY_DIR, "*.json")):
        with open(path, "r", encoding="utf-8") as f:
            j = json.load(f)
        if j.get("status","active") != "active": continue
        SURVEYS[j["key"]] = j

load_surveys()

@dataclass
class Session:
    user_id: int
    survey_key: str
    idx: int = 0
    answers: List[int] = field(default_factory=list)
    lang: str = "ru"

sessions: Dict[int, Session] = {}
user_lang: Dict[int, str] = {}

def lang_kb():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Русский")],[KeyboardButton(text="O‘zbekcha")]], resize_keyboard=True)

def menu_kb(lang:str):
    rows = []
    for key, s in SURVEYS.items():
        rows.append([KeyboardButton(text=s["title"][lang])])
    rows.append([KeyboardButton(text=ABOUT_BUTTON[lang])])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def likert_kb(lang:str):
    row = [InlineKeyboardButton(text=str(i), callback_data=f"ans:{i}") for i in range(1,6)]
    row2 = [InlineKeyboardButton(text=BACK_BUTTON[lang], callback_data="back")]
    return InlineKeyboardMarkup(inline_keyboard=[row,row2])

def share_kb(lang:str):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=SHARE_BUTTON[lang], callback_data="share_hr")],[InlineKeyboardButton(text=HOME_BUTTON[lang], callback_data="home")]])

def resolve_skey_by_title(title:str, lang:str) -> str:
    for key, s in SURVEYS.items():
        if s["title"][lang] == title: return key
    return ""

def score_survey(skey:str, answers:List[int]):
    sdef = SURVEYS[skey]; buckets={}; counts={}
    for ans, item in zip(answers, sdef["items"]):
        val = 6 - ans if item.get("rev") else ans
        k = item["k"]
        buckets[k] = buckets.get(k,0)+val
        counts[k] = counts.get(k,0)+1
    for k in buckets: buckets[k] = round(buckets[k]/counts[k],2)
    top = sorted(buckets.items(), key=lambda x:x[1], reverse=True)[:3]
    return buckets, top

def human_summary(skey:str, scores:dict, top, lang:str):
    sdef = SURVEYS[skey]; labels=sdef["scoring"]
    lines_ru = [f"📊 *{sdef['title']['ru']}*"] + [f"• *{labels[k]['ru']}*: {v}/5" for k,v in scores.items()]
    lines_uz = [f"📊 *{sdef['title']['uz']}*"] + [f"• *{labels[k]['uz']}*: {v}/5" for k,v in scores.items()]
    top_ru = "⭐ Топ: " + ", ".join(f"{labels[k]['ru']} ({v})" for k,v in top)
    top_uz = "⭐ Eng kuchli: " + ", ".join(f"{labels[k]['uz']} ({v})" for k,v in top)
    return "\n".join(lines_ru)+ "\n"+top_ru + "\n\n" + "\n".join(lines_uz)+ "\n"+top_uz

async def ensure_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, lang TEXT, survey_key TEXT, survey_version TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP, raw JSON, scores JSON, shared INTEGER DEFAULT 0
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, lang TEXT);""")
        await db.commit()

async def save_result(user_id:int, lang:str, skey:str, answers:List[int], scores:dict):
    sdef = SURVEYS[skey]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO users(user_id,lang) VALUES(?,?)",(user_id,lang))
        await db.execute("""INSERT INTO results(user_id,lang,survey_key,survey_version,raw,scores)
                            VALUES(?,?,?,?,?,?)""",
                         (user_id, lang, skey, sdef.get("version","unknown"),
                          json.dumps({"answers":answers}, ensure_ascii=False),
                          json.dumps(scores, ensure_ascii=False)))
        await db.commit()

@dp.message(Command("start"))
async def start(m:Message):
    await ensure_db()
    await m.answer(START_TEXT["ru"], reply_markup=lang_kb())

@dp.message(Command("reload"))
async def reload(m:Message):
    if m.from_user.id not in ADMIN_IDS: return
    load_surveys()
    await m.answer("Surveys reloaded.")

@dp.message(F.text == "Русский")
async def set_ru(m:Message):
    user_lang[m.from_user.id] = "ru"
    await m.answer(MENU_TEXT["ru"], reply_markup=menu_kb("ru"))

@dp.message(F.text == "O‘zbekcha")
async def set_uz(m:Message):
    user_lang[m.from_user.id] = "uz"
    await m.answer(MENU_TEXT["uz"], reply_markup=menu_kb("uz"))

@dp.message(F.text.in_([*(pon["title"]["ru"] for pon in SURVEYS.values()),
                        *(pon["title"]["uz"] for pon in SURVEYS.values())]))
async def pick_survey(m:Message):
    lang = user_lang.get(m.from_user.id,"ru")
    skey = resolve_skey_by_title(m.text, lang)
    if not skey: return
    sessions[m.from_user.id] = Session(user_id=m.from_user.id, survey_key=skey, lang=lang)
    await m.answer(CONSENT[lang], reply_markup=InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=AGREE_BUTTON[lang], callback_data="agree")],
                         [InlineKeyboardButton(text=BACK_BUTTON[lang], callback_data="home")]]
    ))

@dp.callback_query(F.data == "agree")
async def agreed(c:CallbackQuery):
    s = sessions.get(c.from_user.id)
    if not s: await c.answer("No session", show_alert=True); return
    s.idx = 0; s.answers = []
    await c.message.answer(SCALE_TEXT[s.lang])
    await ask_next(c.message.chat.id, c.from_user.id)

async def ask_next(chat_id:int, uid:int):
    s = sessions[uid]
    sdef = SURVEYS[s.survey_key]; lang=s.lang
    if s.idx >= len(sdef["items"]):
        scores, top = score_survey(s.survey_key, s.answers)
        await save_result(uid, lang, s.survey_key, s.answers, scores)
        txt = human_summary(s.survey_key, scores, top, lang)
        await bot.send_message(chat_id, txt, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=SHARE_BUTTON[lang], callback_data="share_hr")],
                             [InlineKeyboardButton(text=HOME_BUTTON[lang], callback_data="home")]]
        ))
        sessions.pop(uid, None); return
    item = sdef["items"][s.idx]
    q = f"{s.idx+1}/{len(sdef['items'])}. {item['t'][lang]}"
    await bot.send_message(chat_id, q, reply_markup=likert_kb(lang))

@dp.callback_query(F.data.startswith("ans:"))
async def answer_q(c:CallbackQuery):
    uid = c.from_user.id
    if uid not in sessions: await c.answer("Session not found", show_alert=True); return
    sessions[uid].answers.append(int(c.data.split(":")[1]))
    sessions[uid].idx += 1
    await c.answer(); await ask_next(c.message.chat.id, uid)

@dp.callback_query(F.data == "back")
async def go_back(c:CallbackQuery):
    s = sessions.get(c.from_user.id)
    if not s or s.idx == 0: await c.answer("—"); return
    s.idx -= 1; s.answers.pop()
    await c.answer(); await ask_next(c.message.chat.id, c.from_user.id)

@dp.callback_query(F.data == "share_hr")
async def share_hr(c:CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT ts, survey_key, survey_version, scores FROM results WHERE user_id=? ORDER BY ts DESC LIMIT 1", (c.from_user.id,))
        row = await cur.fetchone()
    if not row: await c.answer("Нет данных", show_alert=True); return
    ts, skey, ver, scores_json = row
    sdef = SURVEYS.get(skey) or {}
    labels = (sdef.get("scoring") or {})
    scores = json.loads(scores_json)
    txt = "👤 @" + (c.from_user.username or str(c.from_user.id)) + "\n" + \
          (sdef.get("title",{}).get("ru","Опрос")) + f" v{ver}\n" + \
          "\n".join(f"• {labels.get(k,{'ru':k})['ru'] if k in labels else k}: {v}/5" for k,v in scores.items())
    for admin in ADMIN_IDS:
        try: await bot.send_message(admin, txt)
        except: pass
    await c.answer("Отправлено HR.", show_alert=True)

@dp.callback_query(F.data == "home")
async def home(c:CallbackQuery):
    lang = user_lang.get(c.from_user.id,"ru")
    await c.message.answer(MENU_TEXT[lang], reply_markup=menu_kb(lang))
    await c.answer()

@dp.message(Command("export"))
async def export_csv(m:Message):
    if m.from_user.id not in ADMIN_IDS: return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT ts,user_id,lang,survey_key,survey_version,raw,scores,shared FROM results ORDER BY ts DESC")
        rows = await cur.fetchall()
    buf = io.StringIO()
    import csv as _csv
    w = _csv.writer(buf); w.writerow(["ts","user_id","lang","survey","version","raw","scores","shared"])
    for r in rows: w.writerow(r)
    buf.seek(0)
    await m.answer_document(FSInputFile.from_file(buf, filename="results.csv"), caption="Экспорт результатов")

async def main():
    await ensure_db()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
