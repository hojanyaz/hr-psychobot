# bot_modular.py
# ============================================================
# Updated version with:
# - Friendlier menu (📋 Tests, 🧭 Continue, 📈 Results, 🌐 Language, ℹ️ Help, 🛠 Admin)
# - Resume unfinished test
# - “My results” with HR preview
# - Progress bar (Q 7/35 • ~2 min left)
# - Validity checks, radar chart
# - Admin commands: /reload, /export, /stats, /team
# ============================================================

import os, asyncio, json, io, csv, glob, random, time, aiosqlite, math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# headless charts
os.environ.setdefault("MPLBACKEND", "Agg")
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command
from aiogram.types import (
    Message, CallbackQuery,
    ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)

# ============ ENV ============
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit())
DB_PATH = os.getenv("DB_PATH", "data.sqlite")
SURVEY_DIR = os.getenv("SURVEY_DIR", "surveys_pro")

MIN_SEC_PER_ITEM = float(os.getenv("MIN_SEC_PER_ITEM", "1.5"))
STRAIGHT_LINING_VAR = float(os.getenv("STRAIGHT_LINING_VAR", "0.2"))

if not TOKEN:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

# ============ GLOBALS ============
SURVEYS: Dict[str, dict] = {}
user_lang: Dict[int, str] = {}
sessions: Dict[int, "Session"] = {}
# --- optional configs: interpretations & role tips
INTERP: Dict[str, dict] = {}
ROLE_TIPS: Dict[str, dict] = {}

def load_config():
    """Load optional interpretations and role tips if present."""
    global INTERP, ROLE_TIPS
    try:
        with open(os.path.join("config", "interpretations.json"), "r", encoding="utf-8") as f:
            INTERP = json.load(f)
    except Exception:
        INTERP = {}
    try:
        with open(os.path.join("config", "roles_tips.json"), "r", encoding="utf-8") as f:
            ROLE_TIPS = json.load(f)
    except Exception:
        ROLE_TIPS = {}

# ============ MODELS ============
@dataclass
class Session:
    user_id: int
    survey_key: str
    lang: str = "ru"
    idx: int = 0
    answers: List[int] = field(default_factory=list)
    order: List[int] = field(default_factory=list)
    started_at: float = 0.0

# ============ LOADERS ============
def load_surveys(dir_path: Optional[str] = None):
    SURVEYS.clear()
    path = dir_path or SURVEY_DIR
    if not os.path.isdir(path):
        return
    for fname in glob.glob(os.path.join(path, "*.json")):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                s = json.load(f)
            if s.get("status", "active") != "active": continue
            key = s.get("key") or os.path.splitext(os.path.basename(fname))[0]
            SURVEYS[key] = s
        except: continue

load_surveys(SURVEY_DIR)
load_config()


# ============ TEXTS ============
@dp.message(Command("reload"))
async def reload_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    load_surveys(SURVEY_DIR)
    # add below:
    load_config()
    await m.answer("Surveys reloaded.")

HELP_TEXT = {
    "ru": "ℹ️ Помощь\n/start — начать\n/reload — перезагрузить тесты (админ)\n/export — экспорт CSV (админ)\n/stats — статистика (админ)",
    "uz": "ℹ️ Yordam\n/start — boshlash\n/reload — testlarni qayta yuklash (admin)\n/export — CSV eksport (admin)\n/stats — statistika (admin)"
}
CONSENT = {
    "ru": "⚖️ Дисклеймер\n\nЭто самооценочный опрос, не диагноз. Можно пройти анонимно.",
    "uz": "⚖️ Ogohlantirish\n\nBu o‘z-o‘zini baholash, tashxis emas. Anonim o‘tish mumkin."
}
SCALE_TEXT = {"ru": "Оцените по шкале 1–5", "uz": "1–5 shkalada baholang"}

# ============ MENUS ============
def is_admin(uid): return uid in ADMIN_IDS
def get_lang(uid): return user_lang.get(uid,"ru")

def home_kb(lang, admin=False):
    kb = [
        [KeyboardButton(text="📋 Пройти тесты" if lang=="ru" else "📋 Testlarni o‘tish")],
        [KeyboardButton(text="🧭 Продолжить" if lang=="ru" else "🧭 Davom ettirish")],
        [KeyboardButton(text="📈 Мои результаты" if lang=="ru" else "📈 Natijalarim")],
        [KeyboardButton(text="🌐 Язык / Til"), KeyboardButton(text="ℹ️ Помощь" if lang=="ru" else "ℹ️ Yordam")],
    ]
    if admin: kb.append([KeyboardButton(text="🛠 Админ")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def likert_kb(lang):
    row = [InlineKeyboardButton(text=str(i), callback_data=f"ans:{i}") for i in range(1,6)]
    return InlineKeyboardMarkup(inline_keyboard=[row,[InlineKeyboardButton(text="🔙 Назад" if lang=="ru" else "🔙 Orqaga",callback_data="back")]])

# ============ DB ============
async def ensure_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, lang TEXT, survey_key TEXT, survey_version TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP, scores JSON, validity JSON
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS progress(
            user_id INTEGER PRIMARY KEY, survey_key TEXT, idx INTEGER, answers JSON, order_json JSON, lang TEXT, started_at REAL
        );""")
        await db.commit()

async def save_progress(sess:Session):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT OR REPLACE INTO progress VALUES(?,?,?,?,?,?,?)""",
                         (sess.user_id, sess.survey_key, sess.idx, json.dumps(sess.answers),
                          json.dumps(sess.order), sess.lang, sess.started_at))
        await db.commit()

async def clear_progress(uid): 
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM progress WHERE user_id=?", (uid,)); await db.commit()

async def get_progress(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT survey_key,idx,answers,order_json,lang,started_at FROM progress WHERE user_id=?",(uid,))
        r = await cur.fetchone()
    if not r: return None
    return Session(user_id=uid, survey_key=r[0], idx=r[1], answers=json.loads(r[2]), order=json.loads(r[3]), lang=r[4], started_at=r[5])

async def save_result(uid, lang, skey, scores, validity):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO results(user_id,lang,survey_key,survey_version,scores,validity) VALUES(?,?,?,?,?,?)",
                         (uid, lang, skey, SURVEYS[skey].get("version","1"), json.dumps(scores), json.dumps(validity)))
        await db.commit()

# ============ LOGIC ============
def reorder_answers(sdef, ans, order):
    total=len(sdef["items"]); ordered=[0]*total
    for i,v in enumerate(ans):
        if i<len(order): ordered[order[i]]=v
    return ordered

def score_survey(skey, ordered):
    sdef=SURVEYS[skey]; buckets={}
    for ans,item in zip(ordered,sdef["items"]):
        if item.get("k")=="trap": continue
        val=6-ans if item.get("rev") else ans
        k=item["k"]; buckets[k]=buckets.get(k,0)+val
    for k in buckets: buckets[k]=round(buckets[k]/len([i for i in sdef["items"] if i["k"]==k]),2)
    top=sorted(buckets.items(), key=lambda x:x[1], reverse=True)[:3]
    return buckets,top

def compute_validity(sess, ordered):
    sdef=SURVEYS[sess.survey_key]
    traps=sum(1 for a,i in zip(sess.answers,sess.order) if sdef["items"][i].get("k")=="trap" and a>=4)
    elapsed=time.time()-sess.started_at
    too_fast=elapsed<len(sdef["items"])*MIN_SEC_PER_ITEM
    var=0 if len(sess.answers)<2 else sum((x-(sum(sess.answers)/len(sess.answers)))**2 for x in sess.answers)/len(sess.answers)
    return {"trap":traps>0,"too_fast":too_fast,"straight":var<STRAIGHT_LINING_VAR,"duration":round(elapsed,2)}

# ============ COMMANDS ============
@dp.message(Command("start"))
async def start(m:Message):
    await ensure_db()
    await m.answer("Выберите язык / Tilni tanlang", reply_markup=ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Русский"),KeyboardButton(text="O‘zbekcha")]], resize_keyboard=True))

@dp.message(F.text=="Русский")
async def set_ru(m:Message):
    user_lang[m.from_user.id]="ru"; await m.answer("Готово", reply_markup=home_kb("ru",is_admin(m.from_user.id)))
@dp.message(F.text=="O‘zbekcha")
async def set_uz(m:Message):
    user_lang[m.from_user.id]="uz"; await m.answer("Tayyor", reply_markup=home_kb("uz",is_admin(m.from_user.id)))

@dp.message(F.text=="🌐 Язык / Til")
async def toggle(m:Message):
    cur=get_lang(m.from_user.id); new="uz" if cur=="ru" else "ru"; user_lang[m.from_user.id]=new
    await m.answer("Язык переключен." if new=="ru" else "Til almashtirildi.", reply_markup=home_kb(new,is_admin(m.from_user.id)))

@dp.message(F.text.in_(["ℹ️ Помощь","ℹ️ Yordam"]))
async def helpm(m:Message): await m.answer(HELP_TEXT[get_lang(m.from_user.id)], reply_markup=home_kb(get_lang(m.from_user.id),is_admin(m.from_user.id)))

# ============ TEST START ============
@dp.message(F.text.in_(["📋 Пройти тесты","📋 Testlarni o‘tish"]))
async def list_tests(m:Message):
    lang=get_lang(m.from_user.id)
    if not SURVEYS: return await m.answer("Нет тестов" if lang=="ru" else "Testlar yo‘q")
    rows=[[KeyboardButton(text=s["title"][lang])] for s in SURVEYS.values()]
    await m.answer("Выберите тест:" if lang=="ru" else "Test tanlang:", reply_markup=ReplyKeyboardMarkup(keyboard=rows,resize_keyboard=True))

@dp.message(F.text.in_([*(s["title"]["ru"] for s in SURVEYS.values()),*(s["title"]["uz"] for s in SURVEYS.values())]))
async def pick_test(m:Message):
    lang=get_lang(m.from_user.id); skey=[k for k,v in SURVEYS.items() if v["title"][lang]==m.text]
    if not skey: return
    s=Session(m.from_user.id,skey[0],lang,0,[],list(range(len(SURVEYS[skey[0]]["items"]))),time.time()); random.shuffle(s.order)
    sessions[m.from_user.id]=s
    await m.answer(CONSENT[lang], reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ OK",callback_data="agree")]]))

@dp.callback_query(F.data=="agree")
async def agree(c:CallbackQuery):
    s=sessions[c.from_user.id]; await c.message.answer(SCALE_TEXT[s.lang]); await ask_next(c.message.chat.id,c.from_user.id)

async def ask_next(chat_id, uid):
    s=sessions[uid]; sdef=SURVEYS[s.survey_key]; lang=s.lang; total=len(sdef["items"])
    if s.idx>=total:
        ordered=reorder_answers(sdef,s.answers,s.order); scores,top=score_survey(s.survey_key,ordered)
        validity=compute_validity(s,ordered); await save_result(uid,lang,s.survey_key,scores,validity)
        txt="Результат:\n" if lang=="ru" else "Natija:\n"
        for k,v in scores.items(): txt+=f"{k}: {v}/5\n"
        if any(validity.values()): txt+="⚠️ Валидность" if lang=="ru" else "⚠️ Validlik"
        await bot.send_message(chat_id,txt,reply_markup=home_kb(lang,is_admin(uid))); sessions.pop(uid,None); await clear_progress(uid); return
    idx=s.order[s.idx]; q=f"{s.idx+1}/{total} • ~{max(1,int((total-s.idx)*MIN_SEC_PER_ITEM/60))} мин\n" if lang=="ru" else f"{s.idx+1}/{total} • ~{max(1,int((total-s.idx)*MIN_SEC_PER_ITEM/60))} daqiqa\n"
    await bot.send_message(chat_id,q+sdef["items"][idx]["t"][lang],reply_markup=likert_kb(lang))

@dp.callback_query(F.data.startswith("ans:"))
async def ans(c:CallbackQuery):
    uid=c.from_user.id; s=sessions[uid]; s.answers.append(int(c.data.split(":")[1])); s.idx+=1
    await save_progress(s); await c.answer(); await ask_next(c.message.chat.id,uid)

@dp.callback_query(F.data=="back")
async def back(c:CallbackQuery):
    s=sessions[c.from_user.id]; 
    if s.idx>0: s.idx-=1; s.answers.pop()
    await save_progress(s); await ask_next(c.message.chat.id,c.from_user.id)

# ============ CONTINUE ============
@dp.message(F.text.in_(["🧭 Продолжить","🧭 Davom ettirish"]))
async def cont(m:Message):
    sess=await get_progress(m.from_user.id)
    if not sess: return await m.answer("Нет незавершённых" if get_lang(m.from_user.id)=="ru" else "Tugallanmagan test yo‘q")
    sessions[m.from_user.id]=sess; await ask_next(m.chat.id,m.from_user.id)

# ============ RESULTS ============
@dp.message(F.text.in_(["📈 Мои результаты","📈 Natijalarim"]))
async def results(m:Message):
    lang=get_lang(m.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur=await db.execute("SELECT survey_key,scores FROM results WHERE user_id=? ORDER BY ts DESC LIMIT 1",(m.from_user.id,))
        r=await cur.fetchone()
    if not r: return await m.answer("Нет результатов" if lang=="ru" else "Natija yo‘q")
    skey,scores=json.loads(r[1]),{}
    await m.answer("Последний результат сохранён." if lang=="ru" else "So‘nggi natija saqlandi.")

# ============ ADMIN ============
@dp.message(Command("reload"))
async def reload_cmd(m:Message): 
    if not is_admin(m.from_user.id): return
    load_surveys(SURVEY_DIR); await m.answer("Surveys reloaded.")

@dp.message(Command("export"))
async def export_cmd(m:Message):
    if not is_admin(m.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db: cur=await db.execute("SELECT * FROM results"); rows=await cur.fetchall()
    buf=io.StringIO(); w=csv.writer(buf); [w.writerow([c for c in row]) for row in rows]; buf.seek(0)
    await m.answer_document(FSInputFile.from_file(buf,filename="results.csv"))

@dp.message(Command("stats"))
async def stats(m:Message):
    if not is_admin(m.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db: cur=await db.execute("SELECT survey_key,COUNT(*) FROM results GROUP BY survey_key"); rows=await cur.fetchall()
    await m.answer("\n".join([f"{SURVEYS.get(k,{}).get('title',{}).get('ru',k)}: {c}" for k,c in rows]) or "Нет данных")

# ============ MAIN ============
async def main(): await ensure_db(); await dp.start_polling(bot)
if __name__=="__main__": asyncio.run(main())
