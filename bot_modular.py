import os, asyncio, json, io, csv, glob, random, time, aiosqlite, math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

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

# ========= ENV & GLOBALS =========
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
ADMIN_IDS = set(int(x) for x in os.getenv("ADMIN_IDS","" ).split(",") if x.strip().isdigit())
DB_PATH = os.getenv("DB_PATH", "data.sqlite")
SURVEY_DIR = os.getenv("SURVEY_DIR", "surveys_pro")
MIN_SEC_PER_ITEM = float(os.getenv("MIN_SEC_PER_ITEM", "1.5"))
STRAIGHT_LINING_VAR = float(os.getenv("STRAIGHT_LINING_VAR", "0.2"))
if not TOKEN:
    raise SystemExit("Please set TELEGRAM_BOT_TOKEN")

bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

SURVEYS: Dict[str, dict] = {}
INTERP: Dict[str, dict] = {}
ROLE_TIPS: Dict[str, dict] = {}
user_lang: Dict[int, str] = {}
sessions: Dict[int, "Session"] = {}
LAST_RESULT: Dict[int, dict] = {}

@dataclass
class Session:
    user_id: int
    survey_key: str
    lang: str = "ru"
    idx: int = 0
    answers: List[int] = field(default_factory=list)
    order: List[int] = field(default_factory=list)
    started_at: float = 0.0

# ========= LOADING =========
def load_surveys(dir_path: Optional[str] = None):
    SURVEYS.clear()
    path = dir_path or SURVEY_DIR
    if not os.path.isdir(path):
        return
    for fname in glob.glob(os.path.join(path, "*.json")):
        try:
            with open(fname, "r", encoding="utf-8") as f:
                s = json.load(f)
            if s.get("status", "active") != "active":
                continue
            key = s.get("key") or os.path.splitext(os.path.basename(fname))[0]
            SURVEYS[key] = s
        except Exception:
            continue

def load_config():
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

load_surveys(SURVEY_DIR)
load_config()

# ========= DB HELPERS =========
async def ensure_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            lang TEXT, role TEXT
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, lang TEXT, survey_key TEXT, survey_version TEXT,
            ts DATETIME DEFAULT CURRENT_TIMESTAMP, scores JSON, validity JSON
        );""")
        await db.execute("""CREATE TABLE IF NOT EXISTS progress(
            user_id INTEGER PRIMARY KEY, survey_key TEXT, idx INTEGER,
            answers JSON, order_json JSON, lang TEXT, started_at REAL
        );""")
        await db.commit()

async def set_user_lang(uid:int, lang:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users(user_id,lang,role) VALUES(?,?,COALESCE((SELECT role FROM users WHERE user_id=?),NULL)) ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
                         (uid, lang, uid))
        await db.commit()

async def set_user_role(uid:int, role:str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users(user_id,role,lang) VALUES(?,?,COALESCE((SELECT lang FROM users WHERE user_id=?),'ru')) ON CONFLICT(user_id) DO UPDATE SET role=excluded.role",
                         (uid, role, uid))
        await db.commit()

async def get_user_role(uid:int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE user_id=?", (uid,))
        row = await cur.fetchone()
    return row[0] if row and row[0] else None

# ========= MENUS =========
def is_admin(uid): return uid in ADMIN_IDS
def get_lang(uid): return user_lang.get(uid, "ru")

def home_kb(lang, admin=False):
    kb = [
        [KeyboardButton(text="📋 Пройти тесты" if lang=="ru" else "📋 Testlarni o‘tish"),
         KeyboardButton(text="🧭 Продолжить" if lang=="ru" else "🧭 Davom ettirish")],
        [KeyboardButton(text="📈 Мои результаты" if lang=="ru" else "📈 Natijalarim"),
         KeyboardButton(text="💼 Выбрать роль" if lang=="ru" else "💼 Rol tanlash")],
        [KeyboardButton(text="🌐 Язык / Til"),
         KeyboardButton(text="ℹ️ Помощь" if lang=="ru" else "ℹ️ Yordam")]
    ]
    if admin:
        kb.append([KeyboardButton(text="🛠 Админ")])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def likert_kb(lang):
    row = [InlineKeyboardButton(text=str(i), callback_data=f"ans:{i}") for i in range(1,6)]
    return InlineKeyboardMarkup(inline_keyboard=[row,
        [InlineKeyboardButton(text="🔙 Назад" if lang=="ru" else "🔙 Orqaga", callback_data="back")],
        [InlineKeyboardButton(text="🏠 В меню" if lang=="ru" else "🏠 Menyuga", callback_data="home")]
    ])

# ========= SCORING & VALIDITY =========
def reorder_answers(sdef, ans, order):
    total = len(sdef["items"]); ordered = [0]*total
    for i,v in enumerate(ans):
        if i < len(order): ordered[order[i]] = v
    return ordered

def score_survey(skey, ordered):
    sdef = SURVEYS[skey]; buckets = {}
    for ans, item in zip(ordered, sdef["items"]):
        if item.get("k") == "trap": continue
        val = 6 - ans if item.get("rev") else ans
        k = item["k"]
        buckets[k] = buckets.get(k,0) + val
    for k in buckets:
        denom = len([i for i in sdef["items"] if i["k"]==k]) or 1
        buckets[k] = round(buckets[k]/denom, 2)
    top = sorted(buckets.items(), key=lambda x: x[1], reverse=True)[:3]
    return buckets, top

def compute_validity(sess:Session, ordered):
    sdef = SURVEYS[sess.survey_key]
    traps = sum(1 for a,i in zip(sess.answers, sess.order)
                if sdef["items"][i].get("k")=="trap" and a>=4)
    elapsed = time.time()-sess.started_at
    too_fast = elapsed < len(sdef["items"]) * MIN_SEC_PER_ITEM
    var = 0 if len(sess.answers)<2 else sum(
        (x-(sum(sess.answers)/len(sess.answers)))**2 for x in sess.answers
    )/len(sess.answers)
    straight = var < STRAIGHT_LINING_VAR
    return {"trap": traps>0, "too_fast": too_fast, "straight": straight, "duration": round(elapsed,2)}

# ========= TEXTS =========
HELP_TEXT = {
    "ru": "ℹ️ Помощь
/start — начать
/reload — перезагрузить тесты (админ)
/export — экспорт CSV (админ)
/stats — статистика (админ)
/role — выбрать роль (Sales/Logistics/Finance/R&D/HR/Manager)",
    "uz": "ℹ️ Yordam
/start — boshlash
/reload — testlarni qayta yuklash (admin)
/export — CSV eksport (admin)
/stats — statistika (admin)
/role — rol tanlash (Sales/Logistics/Finance/R&D/HR/Manager)"
}

# ========= BUILDERS =========
def build_short_summary(lang:str, sdef:dict, scores:dict, top:list)->str:
    labels = sdef["scoring"]
    lines = [f"📊 {sdef['title'][lang]}"]
    for k,v in top: lines.append(f"• {labels[k][lang]}: {v}/5")
    lines.append("🔎 Подробнее" if lang=="ru" else "🔎 Batafsil")
    return "
".join(lines)

def build_role_overlay(lang:str, skey:str, top:list, role:Optional[str])->str:
    if not role: return ""
    bucket = ROLE_TIPS.get(skey, {})
    if not bucket: return ""
    out = []
    for k,_ in top:
        r = bucket.get(k, {}).get(role, {})
        if r:
            out.append(f"— {r.get('ru') if lang=='ru' else r.get('uz')}")
    if out:
        header = "💼 Роль — " if lang=="ru" else "💼 Rol — "
        return header + role + ":
" + "
".join(out)
    return ""

def build_detailed(lang:str, skey:str, scores:dict, top:list, validity:dict, role:Optional[str])->str:
    sdef = SURVEYS[skey]; labels = sdef["scoring"]
    lines = [f"📊 {sdef['title'][lang]}"]
    for k,v in scores.items():
        lines.append(f"• {labels[k][lang]}: {v}/5")
    block = []
    for k,_ in top:
        t = INTERP.get(skey,{}).get(k,{}).get(lang,{})
        if t:
            block.append(
                f"
*{labels[k][lang]}*
"
                f"— {t.get('strengths','')}
"
                f"— {t.get('risks','')}
"
                f"— {t.get('tips','')}"
            )
    if block: lines.append("
".join(block))
    overlay = build_role_overlay(lang, skey, top, role)
    if overlay: lines.append("
" + overlay)
    if validity.get("trap") or validity.get("too_fast") or validity.get("straight"):
        lines.append("⚠️ Проверка валидности" if lang=="ru" else "⚠️ Validlik tekshiruvi")
    return "
".join(lines)

# ========= COMMANDS =========
@dp.message(Command("start"))
async def start(m:Message):
    await ensure_db()
    await m.answer("Выберите язык / Tilni tanlang",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="Русский"), KeyboardButton(text="O‘zbekcha")]],
            resize_keyboard=True))

@dp.message(F.text=="Русский")
async def set_ru(m:Message):
    user_lang[m.from_user.id] = "ru"
    await set_user_lang(m.from_user.id, "ru")
    await m.answer("Готово", reply_markup=home_kb("ru", is_admin(m.from_user.id)))

@dp.message(F.text=="O‘zbekcha")
async def set_uz(m:Message):
    user_lang[m.from_user.id] = "uz"
    await set_user_lang(m.from_user.id, "uz")
    await m.answer("Tayyor", reply_markup=home_kb("uz", is_admin(m.from_user.id)))

@dp.message(F.text=="🌐 Язык / Til")
async def toggle_lang(m:Message):
    cur = get_lang(m.from_user.id)
    new = "uz" if cur=="ru" else "ru"
    user_lang[m.from_user.id] = new
    await set_user_lang(m.from_user.id, new)
    await m.answer("Язык переключен." if new=="ru" else "Til almashtirildi.",
                   reply_markup=home_kb(new, is_admin(m.from_user.id)))

@dp.message(Command("role"))
async def pick_role(m:Message):
    lang = get_lang(m.from_user.id)
    roles = ROLE_TIPS.get("roles", ["Sales","Logistics","Finance","R&D","HR","Manager"])
    buttons = [[InlineKeyboardButton(text=r, callback_data=f"role:{r}")] for r in roles]
    buttons.append([InlineKeyboardButton(text=("🏠 В меню" if lang=="ru" else "🏠 Menyuga"), callback_data="home")])
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await m.answer("Выберите вашу роль:" if lang=="ru" else "Rolingizni tanlang:", reply_markup=kb)

@dp.callback_query(F.data.startswith("role:"))
async def set_role_cb(c:CallbackQuery):
    role = c.data.split(":",1)[1]
    await set_user_role(c.from_user.id, role)
    lang = get_lang(c.from_user.id)
    await c.answer("Роль сохранена" if lang=="ru" else "Rol saqlandi", show_alert=True)

# list tests
@dp.message(F.text.in_(["📋 Пройти тесты","📋 Testlarni o‘tish"]))
async def list_tests(m:Message):
    lang = get_lang(m.from_user.id)
    if not SURVEYS:
        return await m.answer("Нет тестов" if lang=="ru" else "Testlar yo‘q", reply_markup=home_kb(lang, is_admin(m.from_user.id)))
    rows = [[KeyboardButton(text=s["title"][lang])] for s in SURVEYS.values()]
    rows.append([KeyboardButton(text=("🏠 В меню" if lang=="ru" else "🏠 Menyuga"))])
    await m.answer("Выберите тест:" if lang=="ru" else "Testni tanlang:",
                   reply_markup=ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True))

# pick test by title
@dp.message(F.text.in_([*(s["title"]["ru"] for s in SURVEYS.values()), *(s["title"]["uz"] for s in SURVEYS.values())]))
async def pick_test(m:Message):
    lang = get_lang(m.from_user.id)
    skey = [k for k,v in SURVEYS.items() if v["title"][lang]==m.text]
    if not skey: return
    s = Session(m.from_user.id, skey[0], lang, 0, [], list(range(len(SURVEYS[skey[0]]["items"]))), time.time())
    random.shuffle(s.order)
    sessions[m.from_user.id] = s
    await m.answer("⚖️ Дисклеймер

Это самооценочный опрос, не диагноз. Можно пройти анонимно." if lang=="ru" else
                   "⚖️ Ogohlantirish

Bu o‘z-o‘zini baholash, tashxis emas. Anonim o‘tish mumkin.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                       [InlineKeyboardButton(text="✅ OK", callback_data="agree")],
                       [InlineKeyboardButton(text=("🏠 В меню" if lang=="ru" else "🏠 Menyuga"), callback_data="home")]
                   ]))

@dp.callback_query(F.data=="agree")
async def agree(c:CallbackQuery):
    s = sessions[c.from_user.id]
    lang = s.lang
    await c.message.answer("Оцените по шкале 1–5" if lang=="ru" else "1–5 shkalada baholang")
    await ask_next(c.message.chat.id, c.from_user.id)

async def ask_next(chat_id, uid:int):
    s = sessions[uid]; sdef = SURVEYS[s.survey_key]; lang = s.lang; total = len(sdef["items"])
    if s.idx >= total:
        ordered = reorder_answers(sdef, s.answers, s.order)
        scores, top = score_survey(s.survey_key, ordered)
        validity = compute_validity(s, ordered)
        await save_result(uid, lang, s.survey_key, scores, validity)
        short_txt = build_short_summary(lang, sdef, scores, top)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=("🔎 Подробнее" if lang=="ru" else "🔎 Batafsil"), callback_data="more")],
            [InlineKeyboardButton(text=("📤 Поделиться HR" if lang=="ru" else "📤 HRga yuborish"), callback_data="send_hr")],
            [InlineKeyboardButton(text=("🏠 В меню" if lang=="ru" else "🏠 Menyuga"), callback_data="home")]
        ])
        await bot.send_message(chat_id, short_txt, reply_markup=kb)
        LAST_RESULT[uid] = {"lang": lang, "skey": s.survey_key, "scores": scores, "top": top, "validity": validity}
        await clear_progress(uid); sessions.pop(uid, None); return

    idx = s.order[s.idx]
    q_text = sdef["items"][idx]["t"][lang]
    await bot.send_message(chat_id, f"{s.idx+1}/{total}
" + q_text, reply_markup=likert_kb(lang))

@dp.callback_query(F.data=="more")
async def more_details(c:CallbackQuery):
    data = LAST_RESULT.get(c.from_user.id)
    lang = get_lang(c.from_user.id)
    if not data:
        return await c.answer("Нет данных" if lang=="ru" else "Ma'lumot yo‘q", show_alert=True)
    role = await get_user_role(c.from_user.id)
    detailed = build_detailed(lang, data["skey"], data["scores"], data["top"], data["validity"], role)
    await c.message.answer(detailed)
    sdef = SURVEYS[data["skey"]]
    chart_path = f"profile_{c.from_user.id}_{int(time.time())}.png"
    labels = [sdef["scoring"][k]['ru'] for k in data["scores"].keys()]
    values = list(data["scores"].values())
    if labels and values:
        angles = [n/float(len(labels))*2*math.pi for n in range(len(labels))]
        values2 = values + values[:1]; angles2 = angles + angles[:1]
        fig = plt.figure(); ax = plt.subplot(polar=True)
        ax.set_xticks(angles); ax.set_xticklabels(labels, fontsize=8)
        ax.set_yticklabels([]); ax.plot(angles2, values2); ax.fill(angles2, values2, alpha=0.1)
        ax.set_title("Профиль (1–5)", fontsize=11)
        fig.savefig(chart_path, bbox_inches="tight"); plt.close(fig)
        await bot.send_photo(c.message.chat.id, photo=FSInputFile(chart_path), caption=("График профиля" if lang=="ru" else "Profil grafigi"))
    await c.answer()

# continue unfinished
@dp.message(F.text.in_(["🧭 Продолжить","🧭 Davom ettirish"]))
async def cont(m:Message):
    sess = await get_progress(m.from_user.id)
    if not sess:
        return await m.answer("Нет незавершённых" if get_lang(m.from_user.id)=="ru" else "Tugallanmagan test yo‘q",
                               reply_markup=home_kb(get_lang(m.from_user.id), is_admin(m.from_user.id)))
    sessions[m.from_user.id] = sess
    await ask_next(m.chat.id, m.from_user.id)

# show last result (short)
@dp.message(F.text.in_(["📈 Мои результаты","📈 Natijalarim"]))
async def my_results(m:Message):
    lang = get_lang(m.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT survey_key,scores,validity FROM results WHERE user_id=? ORDER BY ts DESC LIMIT 1",
                               (m.from_user.id,))
        row = await cur.fetchone()
    if not row:
        return await m.answer("Нет результатов" if lang=="ru" else "Natija yo‘q",
                               reply_markup=home_kb(lang, is_admin(m.from_user.id)))
    skey = row[0]; scores = json.loads(row[1]); validity = json.loads(row[2])
    top = sorted(scores.items(), key=lambda x:x[1], reverse=True)[:3]
    short_txt = build_short_summary(lang, SURVEYS[skey], scores, top)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=("🔎 Подробнее" if lang=="ru" else "🔎 Batafsil"), callback_data="more")],
        [InlineKeyboardButton(text=("📤 Поделиться HR" if lang=="ru" else "📤 HRga yuborish"), callback_data="send_hr")],
        [InlineKeyboardButton(text=("🏠 В меню" if lang=="ru" else "🏠 Menyuga"), callback_data="home")]
    ])
    await m.answer(short_txt, reply_markup=kb)
    LAST_RESULT[m.from_user.id] = {"lang":lang,"skey":skey,"scores":scores,"top":top,"validity":validity}

# Admin quick actions
@dp.message(F.text=="🛠 Админ")
async def admin_menu(m:Message):
    if not is_admin(m.from_user.id): return
    lang = get_lang(m.from_user.id)
    kb = ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="/export"), KeyboardButton(text="/stats")],
        [KeyboardButton(text="/reload"), KeyboardButton(text="/role")],
        [KeyboardButton(text=("🏠 В меню" if lang=="ru" else "🏠 Menyuga"))]
    ], resize_keyboard=True)
    await m.answer("Админ-меню" if lang=="ru" else "Admin menyu", reply_markup=kb)

# Home button handler (from reply keyboards)
@dp.message(F.text.in_(["🏠 В меню","🏠 Menyuga"]))
async def to_home(m:Message):
    lang = get_lang(m.from_user.id)
    await m.answer("Меню" if lang=="ru" else "Menyu", reply_markup=home_kb(lang, is_admin(m.from_user.id)))

# Inline HOME
@dp.callback_query(F.data=="home")
async def to_home_cb(c:CallbackQuery):
    lang = get_lang(c.from_user.id)
    await c.message.answer("Меню" if lang=="ru" else "Menyu", reply_markup=home_kb(lang, is_admin(c.from_user.id)))
    await c.answer()

# admin: reload + export + stats
@dp.message(Command("reload"))
async def reload_cmd(m:Message):
    if not is_admin(m.from_user.id): return
    load_surveys(SURVEY_DIR); load_config()
    await m.answer("Surveys reloaded.")

@dp.message(Command("export"))
async def export_cmd(m:Message):
    if not is_admin(m.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM results"); rows = await cur.fetchall()
    buf = io.StringIO(); w = csv.writer(buf)
    [w.writerow([c for c in row]) for row in rows]; buf.seek(0)
    await m.answer_document(FSInputFile.from_file(buf, filename="results.csv"))

@dp.message(Command("stats"))
async def stats(m:Message):
    if not is_admin(m.from_user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT survey_key,COUNT(*) FROM results GROUP BY survey_key"); rows = await cur.fetchall()
    await m.answer("
".join([f"{SURVEYS.get(k,{}).get('title',{}).get('ru',k)}: {c}" for k,c in rows]) or "Нет данных")

async def main():
    await ensure_db(); await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
