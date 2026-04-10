import asyncio, json, math, os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import aiohttp
import asyncpg
import uvicorn
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query as Q
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
)

load_dotenv()

BOT_TOKEN  = os.getenv("BOT_TOKEN")
DB_URL     = os.getenv("DB_URL")
API_SECRET = os.getenv("API_SECRET", "changeme")

bot       = Bot(token=BOT_TOKEN)
dp        = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()
pool: asyncpg.Pool = None   # set in lifespan


# ════════════════════════════════════════════════════════════
#  DATABASE — INIT + HELPERS
# ════════════════════════════════════════════════════════════
async def init_db():
    async with pool.acquire() as c:
        await c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id          SERIAL PRIMARY KEY,
            telegram_id BIGINT UNIQUE NOT NULL,
            name        TEXT,
            device_id   TEXT,
            created_at  TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS relatives (
            id          SERIAL PRIMARY KEY,
            relative_id BIGINT NOT NULL,
            patient_id  BIGINT NOT NULL,
            role        TEXT DEFAULT 'viewer',    -- viewer | admin
            UNIQUE(relative_id, patient_id)
        );

        CREATE TABLE IF NOT EXISTS pills (
            id              SERIAL PRIMARY KEY,
            user_id         BIGINT NOT NULL,
            name            TEXT NOT NULL,
            dosage          TEXT,
            total_count     INT DEFAULT 0,
            remaining_count INT DEFAULT 0,
            slot            INT NOT NULL,         -- 0-7 номер комірки
            created_at      TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS schedule (
            id      SERIAL PRIMARY KEY,
            pill_id INT REFERENCES pills(id) ON DELETE CASCADE,
            times   TEXT NOT NULL,                -- JSON: ["08:00","20:00"]
            days    TEXT NOT NULL                 -- JSON: [0,1,2,3,4,5,6]
        );

        CREATE TABLE IF NOT EXISTS logs (
            id      SERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL,
            pill_id INT,
            slot    INT,
            event   TEXT NOT NULL,               -- open|taken|remind|missed
            time    TIMESTAMPTZ DEFAULT NOW()
        );
        """)

async def get_day_times(user_id: int):
    """Повертає словник {день: "час"}, щоб знати, коли комірка вже зайнята."""
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT s.times, s.days FROM schedule s JOIN pills p ON s.pill_id = p.id WHERE p.user_id=$1", 
            user_id
        )
    day_times = {}
    for r in rows:
        times = json.loads(r["times"])
        days = json.loads(r["days"])
        if times:
            t = times[0]  # Беремо єдиний час прийому
            for d in days:
                day_times[d] = t
    return day_times


async def ensure_user(telegram_id: int, name: str):
    async with pool.acquire() as c:
        await c.execute(
            "INSERT INTO users(telegram_id, name) VALUES($1,$2) ON CONFLICT DO NOTHING",
            telegram_id, name,
        )


async def get_user(telegram_id: int):
    async with pool.acquire() as c:
        return await c.fetchrow("SELECT * FROM users WHERE telegram_id=$1", telegram_id)


async def get_pills(user_id: int):
    async with pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM pills WHERE user_id=$1 ORDER BY id", user_id
        )
        
async def get_relatives(patient_id: int):
    async with pool.acquire() as c:
        return await c.fetch(
            "SELECT * FROM relatives WHERE patient_id=$1", patient_id
        )


async def notify_relatives(patient_id: int, text: str, min_role: str = "viewer"):
    """Send message to all relatives with role >= min_role."""
    roles = ["viewer", "admin"] if min_role == "viewer" else ["admin"]
    async with pool.acquire() as c:
        rels = await c.fetch(
            "SELECT relative_id FROM relatives WHERE patient_id=$1 AND role=ANY($2)",
            patient_id, roles,
        )
    for r in rels:
        try:
            await bot.send_message(r["relative_id"], text, parse_mode="HTML")
        except Exception:
            pass

def haversine(lat1, lon1, lat2, lon2) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


DAYS_UA = {0: "Пн", 1: "Вт", 2: "Ср", 3: "Чт", 4: "Пт", 5: "Сб", 6: "Нд"}


# ════════════════════════════════════════════════════════════
#  FSM STATES
# ════════════════════════════════════════════════════════════
class AddPill(StatesGroup):
    name   = State()
    dosage = State()
    count  = State()
    times  = State()
    days   = State()


class EditPill(StatesGroup):
    choose_pill  = State()
    choose_field = State()
    new_value    = State()


class LinkDevice(StatesGroup):
    waiting_id = State()


# ════════════════════════════════════════════════════════════
#  KEYBOARDS
# ════════════════════════════════════════════════════════════
def main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💊 Мої ліки"),      KeyboardButton(text="📅 Розклад")],
            [KeyboardButton(text="📝 Інструкція на тиждень"), KeyboardButton(text="📊 Статистика")],
            [KeyboardButton(text="📖 Історія"),         KeyboardButton(text="🏪 Аптека поруч")],
            [KeyboardButton(text="👨‍👩‍👧 Родичі"),       KeyboardButton(text="⚙️ Налаштування")],
            [KeyboardButton(text="🔄 Синхронізація"), KeyboardButton(text="🔥 Серія")],
        ],
        resize_keyboard=True,
    )


def cancel_kb():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Скасувати")]],
        resize_keyboard=True,
    )


def location_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 Надіслати геолокацію", request_location=True)],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )


def generate_days_kb(selected: set, day_times: dict, current_time: str):
    kb = []
    row = []
    for d, name in DAYS_UA.items():
        # Якщо в цей день ВЖЕ є ліки, і їх час НЕ СПІВПАДАЄ з тим, що вводить користувач
        if d in day_times and day_times[d] != current_time:
            btn_text = f"⛔ {name} ({day_times[d]})"
            cb_data = f"dblocked_{d}"
        else:
            mark = "✅ " if d in selected else ""
            btn_text = f"{mark}{name}"
            cb_data = f"dtoggle_{d}"
            
        row.append(InlineKeyboardButton(text=btn_text, callback_data=cb_data))
        if len(row) == 3:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    
    kb.append([InlineKeyboardButton(text="🔄 Вибрати всі вільні/доступні", callback_data="dtoggle_all")])
    kb.append([InlineKeyboardButton(text="✅ Підтвердити", callback_data="dtoggle_done")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


async def pills_inline_kb(user_id: int, prefix: str):
    pills = await get_pills(user_id)
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💊 {p['name']}",
                callback_data=f"{prefix}_{p['id']}",
            )]
            for p in pills
        ]
    )


# ════════════════════════════════════════════════════════════
#  /start
# ════════════════════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    args = message.text.split()

    # Relative deep link: /start patient_<id>_<role>
    if len(args) > 1 and args[1].startswith("patient_"):
        parts = args[1].split("_")
        patient_id = int(parts[1])
        role = parts[2] if len(parts) > 2 else "viewer"
        await ensure_user(message.chat.id, message.from_user.first_name)
        async with pool.acquire() as c:
            await c.execute(
                "INSERT INTO relatives(relative_id, patient_id, role) "
                "VALUES($1,$2,$3) ON CONFLICT DO NOTHING",
                message.chat.id, patient_id, role,
            )
        role_ua = "адмін" if role == "admin" else "спостерігач"
        return await message.answer(
            f"✅ Ви підключені як родич ({role_ua})!\n"
            f"Отримуватимете сповіщення про прийом ліків.",
            reply_markup=main_kb(),
        )

    await ensure_user(message.chat.id, message.from_user.first_name)
    await message.answer(
        f"👋 Вітаю, {message.from_user.first_name}!\n\n"
        f"Ваш ID: <code>{message.chat.id}</code>\n\n"
        f"Для початку підключіть пристрій через ⚙️ Налаштування → /link_device",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


# ════════════════════════════════════════════════════════════
#  /link_device
# ════════════════════════════════════════════════════════════
@dp.message(Command("link_device"))
async def cmd_link_device(message: types.Message, state: FSMContext):
    await state.set_state(LinkDevice.waiting_id)
    await message.answer(
        "🔌 Введіть ID пристрою (вказано в Serial Monitor при старті ESP32):",
        reply_markup=cancel_kb(),
    )


@dp.message(LinkDevice.waiting_id)
async def link_device_done(message: types.Message, state: FSMContext):
    if message.text == "❌ Скасувати":
        await state.clear()
        return await message.answer("Скасовано.", reply_markup=main_kb())
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE users SET device_id=$1 WHERE telegram_id=$2",
            message.text.strip(), message.chat.id,
        )
    await state.clear()
    await message.answer(
        f"✅ Пристрій <code>{message.text.strip()}</code> підключено!",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


# ════════════════════════════════════════════════════════════
#  /add_pill  — FSM
# ════════════════════════════════════════════════════════════
@dp.message(Command("add_pill"))
async def cmd_add_pill(message: types.Message, state: FSMContext):
    await state.set_state(AddPill.name)
    await message.answer("💊 Назва ліку (наприклад: Аспірин):", reply_markup=cancel_kb())


@dp.message(AddPill.name)
async def ap_name(message: types.Message, state: FSMContext):
    if message.text == "❌ Скасувати":
        await state.clear(); return await message.answer("Скасовано.", reply_markup=main_kb())
    await state.update_data(name=message.text.strip())
    await state.set_state(AddPill.dosage)
    await message.answer("📏 Доза (наприклад: 500 мг або 1 табл.):")


@dp.message(AddPill.dosage)
async def ap_dosage(message: types.Message, state: FSMContext):
    if message.text == "❌ Скасувати":
        await state.clear(); return await message.answer("Скасовано.", reply_markup=main_kb())
    await state.update_data(dosage=message.text.strip())
    await state.set_state(AddPill.count)
    await message.answer("🔢 Кількість таблеток в упаковці:")


@dp.message(AddPill.count)
async def ap_count(message: types.Message, state: FSMContext):
    if message.text == "❌ Скасувати":
        await state.clear(); return await message.answer("Скасовано.", reply_markup=main_kb())
    if not message.text.strip().isdigit():
        return await message.answer("⚠️ Введіть ціле число:")
    await state.update_data(count=int(message.text.strip()))
    
    await state.set_state(AddPill.times)
    await message.answer("⏰ Введіть час прийому (тільки ОДИН час, наприклад: 09:00):")


@dp.message(AddPill.times)
async def ap_times(message: types.Message, state: FSMContext):
    if message.text == "❌ Скасувати":
        await state.clear(); return await message.answer("Скасовано.", reply_markup=main_kb())
    
    raw_text = message.text.replace(".", ":").strip()
    try:
        datetime.strptime(raw_text, "%H:%M")
    except ValueError:
        return await message.answer("⚠️ Формат має бути HH:MM. Приклад: 09:00")
    
    # Отримуємо зайняті дні перед генерацією клавіатури
    day_times = await get_day_times(message.chat.id)
    
    await state.update_data(times=[raw_text], selected_days=[], day_times=day_times)
    await state.set_state(AddPill.days)
    
    await message.answer(
        f"📆 Оберіть дні прийому для <b>{raw_text}</b>:\n"
        f"<i>(Дні, в яких вже встановлено ІНШИЙ час, заблоковані ⛔)</i>", 
        reply_markup=generate_days_kb(set(), day_times, raw_text),
        parse_mode="HTML"
    )

# Обробник для заблокованих кнопок
@dp.callback_query(AddPill.days, F.data.startswith("dblocked_"))
async def ap_days_blocked(callback: types.CallbackQuery):
    day = int(callback.data.split("_")[1])
    await callback.answer(
        f"⚠️ У {DAYS_UA[day]} вже є ліки на інший час! В один день може бути лише один час.", 
        show_alert=True
    )

# Обробник для доступних кнопок
@dp.callback_query(AddPill.days, F.data.startswith("dtoggle_"))
async def ap_days_toggle(callback: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    selected = set(data.get("selected_days", []))
    day_times = data.get("day_times", {})
    current_time = data["times"][0]
    action = callback.data.split("_")[1]
    
    if action == "done":
        if not selected:
            return await callback.answer("⚠️ Оберіть хоча б один день!", show_alert=True)
        await callback.message.edit_reply_markup(reply_markup=None)
        await _save_pill(callback.message, state, list(selected))
        await callback.answer()
        return
        
    if action == "all":
        # Вибираємо тільки ті дні, які вільні АБО мають такий самий час
        possible_days = {d for d in range(7) if d not in day_times or day_times[d] == current_time}
        if len(selected) == len(possible_days):
            selected = set()
        else:
            selected = possible_days
    else:
        day = int(action)
        if day in selected:
            selected.remove(day)
        else:
            selected.add(day)
            
    await state.update_data(selected_days=list(selected))
    await callback.message.edit_reply_markup(
        reply_markup=generate_days_kb(selected, day_times, current_time)
    )
    await callback.answer()


async def _save_pill(message: types.Message, state: FSMContext, days: list):
    data = await state.get_data()
    async with pool.acquire() as c:
        pill_id = await c.fetchval(
            """INSERT INTO pills(user_id, name, dosage, total_count, remaining_count, slot)
               VALUES($1,$2,$3,$4,$4,$5) RETURNING id""",
            message.chat.id, data["name"], data["dosage"], data["count"], 0,
        )
        await c.execute(
            "INSERT INTO schedule(pill_id, times, days) VALUES($1,$2,$3)",
            pill_id, json.dumps(data["times"]), json.dumps(days),
        )
    await state.clear()
    days_str = ", ".join(DAYS_UA[d] for d in sorted(days))
    await message.answer(
        f"✅ <b>{data['name']}</b> додано!\n\n"
        f"📏 Доза: {data['dosage']}\n"
        f"⏰ Час: {', '.join(data['times'])}\n"
        f"📆 Дні: {days_str}\n"
        f"📦 Залишок: {data['count']} шт.\n\n"
        f"Натисніть 🔄 Синхронізація щоб оновити пристрій.",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


# ════════════════════════════════════════════════════════════
#  💊 МОЇ ЛІКИ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "💊 Мої ліки")
@dp.message(Command("my_pills"))
async def my_pills(message: types.Message):
    pills = await get_pills(message.chat.id)
    if not pills:
        return await message.answer("📭 Ліків немає. Додайте через /add_pill")

    text = "💊 <b>Ваші ліки:</b>\n\n"
    async with pool.acquire() as c:
        for p in pills:
            s = await c.fetchrow("SELECT * FROM schedule WHERE pill_id=$1", p["id"])
            times_str = ", ".join(json.loads(s["times"])) if s else "—"
            pct = int(p["remaining_count"] / p["total_count"] * 100) if p["total_count"] else 0
            bar = "🟩" * (pct // 20) + "⬜" * (5 - pct // 20)
            text += (
                f"🔹 <b>{p['name']}</b> {p['dosage']}\n"
                f"  ⏰ {times_str}\n"
                f"  {bar} {p['remaining_count']}/{p['total_count']} шт.\n\n"
            )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Редагувати", callback_data="go_edit"),
             InlineKeyboardButton(text="🗑 Видалити",   callback_data="go_delete")],
            [InlineKeyboardButton(text="📦 Поповнити",  callback_data="go_refill")],
        ]
    )
    await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ════════════════════════════════════════════════════════════
#  EDIT PILL — FSM
# ════════════════════════════════════════════════════════════
@dp.callback_query(F.data == "go_edit")
@dp.message(Command("edit_pill"))
async def cmd_edit_pill(event, state: FSMContext):
    msg = event if isinstance(event, types.Message) else event.message
    uid = event.from_user.id
    pills = await get_pills(uid)
    if not pills:
        return await msg.answer("📭 Немає ліків для редагування.")
    await state.set_state(EditPill.choose_pill)
    await msg.answer("Оберіть ліки:", reply_markup=await pills_inline_kb(uid, "editp"))
    if isinstance(event, types.CallbackQuery):
        await event.answer()


@dp.callback_query(EditPill.choose_pill, F.data.startswith("editp_"))
async def edit_choose_field(callback: types.CallbackQuery, state: FSMContext):
    pill_id = int(callback.data.split("_")[1])
    await state.update_data(pill_id=pill_id)
    await state.set_state(EditPill.choose_field)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Назва",         callback_data="ef_name")],
            [InlineKeyboardButton(text="Доза",          callback_data="ef_dosage")],
            [InlineKeyboardButton(text="Час прийому",   callback_data="ef_times")],
            [InlineKeyboardButton(text="Кількість",     callback_data="ef_count")],
        ]
    )
    await callback.message.answer("Що змінити?", reply_markup=kb)
    await callback.answer()


@dp.callback_query(EditPill.choose_field, F.data.startswith("ef_"))
async def edit_enter_value(callback: types.CallbackQuery, state: FSMContext):
    field = callback.data.split("_")[1]
    await state.update_data(field=field)
    await state.set_state(EditPill.new_value)
    prompts = {
        "name":   "Введіть нову назву:",
        "dosage": "Введіть нову дозу:",
        "times":  "Введіть новий час (08:00, 20:00):",
        "count":  "Введіть нову кількість таблеток:",
    }
    await callback.message.answer(prompts[field], reply_markup=cancel_kb())
    await callback.answer()


@dp.message(EditPill.new_value)
async def edit_save(message: types.Message, state: FSMContext):
    if message.text == "❌ Скасувати":
        await state.clear(); return await message.answer("Скасовано.", reply_markup=main_kb())
    data = await state.get_data()
    field, pill_id = data["field"], data["pill_id"]
    async with pool.acquire() as c:
        if field == "times":
            times = [t.strip() for t in message.text.split(",")]
            await c.execute(
                "UPDATE schedule SET times=$1 WHERE pill_id=$2",
                json.dumps(times), pill_id,
            )
        elif field == "count":
            if not message.text.strip().isdigit():
                return await message.answer("⚠️ Введіть ціле число:")
            n = int(message.text.strip())
            await c.execute(
                "UPDATE pills SET total_count=$1, remaining_count=$1 WHERE id=$2",
                n, pill_id,
            )
        else:
            await c.execute(
                f"UPDATE pills SET {field}=$1 WHERE id=$2",
                message.text.strip(), pill_id,
            )
    await state.clear()
    await message.answer(
        "✅ Збережено! Натисніть 🔄 Синхронізація щоб оновити пристрій.",
        reply_markup=main_kb(),
    )


# ════════════════════════════════════════════════════════════
#  DELETE PILL
# ════════════════════════════════════════════════════════════
@dp.callback_query(F.data == "go_delete")
@dp.message(Command("delete_pill"))
async def cmd_delete_pill(event, state: FSMContext):
    msg = event if isinstance(event, types.Message) else event.message
    uid = event.from_user.id
    pills = await get_pills(uid)
    if not pills:
        return await msg.answer("📭 Немає ліків.")
    await msg.answer("Оберіть ліки для видалення:", reply_markup=await pills_inline_kb(uid, "del"))
    if isinstance(event, types.CallbackQuery):
        await event.answer()


@dp.callback_query(F.data.startswith("del_"))
async def delete_confirm(callback: types.CallbackQuery):
    pill_id = int(callback.data.split("_")[1])
    async with pool.acquire() as c:
        p = await c.fetchrow("SELECT * FROM pills WHERE id=$1", pill_id)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✅ Так, видалити", callback_data=f"delok_{pill_id}"),
            InlineKeyboardButton(text="❌ Ні",            callback_data="delno"),
        ]]
    )
    await callback.message.answer(
        f"Видалити <b>{p['name']}</b>?",
        parse_mode="HTML", reply_markup=kb,
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("delok_"))
async def delete_execute(callback: types.CallbackQuery):
    pill_id = int(callback.data.split("_")[1])
    async with pool.acquire() as c:
        await c.execute("DELETE FROM pills WHERE id=$1", pill_id)
    await callback.message.answer("✅ Видалено.", reply_markup=main_kb())
    await callback.answer()


@dp.callback_query(F.data == "delno")
async def delete_cancel(callback: types.CallbackQuery):
    await callback.message.answer("Скасовано.", reply_markup=main_kb())
    await callback.answer()


# ════════════════════════════════════════════════════════════
#  REFILL
# ════════════════════════════════════════════════════════════
@dp.callback_query(F.data == "go_refill")
@dp.message(Command("refill"))
async def cmd_refill(event, state: FSMContext):
    msg = event if isinstance(event, types.Message) else event.message
    uid = event.from_user.id
    pills = await get_pills(uid)
    if not pills:
        return await msg.answer("📭 Немає ліків.")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(
                text=f"💊 {p['name']}",
                callback_data=f"rfill_{p['id']}_{p['total_count']}",
            )]
            for p in pills
        ]
    )
    await msg.answer("Яке ліко поповнили?", reply_markup=kb)
    if isinstance(event, types.CallbackQuery):
        await event.answer()


@dp.callback_query(F.data.startswith("rfill_"))
async def refill_execute(callback: types.CallbackQuery):
    _, pill_id, total = callback.data.split("_")
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE pills SET remaining_count=$1 WHERE id=$2",
            int(total), int(pill_id),
        )
    await callback.message.answer(
        f"✅ Залишок відновлено до {total} шт.", reply_markup=main_kb()
    )
    await callback.answer()


# ════════════════════════════════════════════════════════════
#  📅 РОЗКЛАД
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "📅 Розклад")
async def show_schedule(message: types.Message):
    pills = await get_pills(message.chat.id)
    if not pills:
        return await message.answer("📭 Ліків немає.")
    text = "📅 <b>Розклад прийому:</b>\n\n"
    async with pool.acquire() as c:
        for p in pills:
            s = await c.fetchrow("SELECT * FROM schedule WHERE pill_id=$1", p["id"])
            if not s:
                continue
            times = ", ".join(json.loads(s["times"]))
            days  = ", ".join(DAYS_UA[d] for d in sorted(json.loads(s["days"])))
            text += (
                f"🔹 <b>{p['name']}</b> {p['dosage']}\n"
                f"  ⏰ {times}\n"
                f"  📆 {days}\n\n"
            )
    await message.answer(text, parse_mode="HTML")


# ════════════════════════════════════════════════════════════
#  📝 ІНСТРУКЦІЯ НА ТИЖДЕНЬ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "📝 Інструкція на тиждень")
@dp.message(Command("instruction"))
async def generate_weekly_instruction(message: types.Message):
    days_names = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
    
    async with pool.acquire() as c:
        pills = await c.fetch(
            "SELECT p.*, s.days, s.times FROM pills p "
            "JOIN schedule s ON p.id = s.pill_id "
            "WHERE p.user_id=$1", 
            message.chat.id
        )
        
    if not pills:
        return await message.answer("📭 У вас немає доданих ліків. Додайте їх через Налаштування.")

    instruction = "📝 <b>Ваша інструкція розкладки на тиждень:</b>\n\n"
    instruction += "<i>Відкрийте всі 7 комірок і розкладіть ліки згідно зі списком:</i>\n\n"
    
    for day_idx in range(7):
        slot_num = day_idx + 1
        day_pills = []
        
        for p in pills:
            days = json.loads(p["days"])
            if day_idx in days:
                times = ", ".join(json.loads(p["times"]))
                day_pills.append(f"  💊 {p['name']} ({p['dosage']}) — на {times}")
                
        instruction += f"🗂 <b>Комірка {slot_num} ({days_names[day_idx]}):</b>\n"
        if day_pills:
            instruction += "\n".join(day_pills) + "\n\n"
        else:
            instruction += "  <i>Пусто (прийомів немає)</i>\n\n"
            
    instruction += "✅ <b>Готово!</b> Закрийте кришку. Система сама нагадає вам про прийом у потрібний день та час."
    
    await message.answer(instruction, parse_mode="HTML")


# ════════════════════════════════════════════════════════════
#  🔄 СИНХРОНІЗАЦІЯ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "🔄 Синхронізація")
@dp.message(Command("sync"))
async def cmd_sync(message: types.Message):
    user = await get_user(message.chat.id)
    device = user["device_id"] if user and user["device_id"] else "не підключено"
    await message.answer(
        f"✅ Розклад у базі оновлено.\n\n"
        f"ESP32 (<code>{device}</code>) завантажить новий розклад при наступному старті "
        f"або одразу по Wi-Fi через <code>GET /api/schedule?device_id={device}</code>",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )


# ════════════════════════════════════════════════════════════
#  📊 СТАТИСТИКА
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "📊 Статистика")
@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    async with pool.acquire() as c:
        week_ago = datetime.now() - timedelta(days=7)
        taken  = await c.fetchval(
            "SELECT COUNT(*) FROM logs WHERE user_id=$1 AND event='taken'  AND time>$2",
            message.chat.id, week_ago,
        )
        missed = await c.fetchval(
            "SELECT COUNT(*) FROM logs WHERE user_id=$1 AND event='missed' AND time>$2",
            message.chat.id, week_ago,
        )

        # Per-pill breakdown
        pills = await get_pills(message.chat.id)
        breakdown = ""
        for p in pills:
            pt = await c.fetchval(
                "SELECT COUNT(*) FROM logs WHERE user_id=$1 AND pill_id=$2 AND event='taken' AND time>$3",
                message.chat.id, p["id"], week_ago,
            )
            pm = await c.fetchval(
                "SELECT COUNT(*) FROM logs WHERE user_id=$1 AND pill_id=$2 AND event='missed' AND time>$3",
                message.chat.id, p["id"], week_ago,
            )
            total_p = pt + pm
            pct_p   = int(pt / total_p * 100) if total_p else 0
            breakdown += f"  • {p['name']}: {pt}/{total_p} ({pct_p}%)\n"

    total = taken + missed
    pct   = int(taken / total * 100) if total else 0
    bar   = "🟩" * (pct // 10) + "⬜" * (10 - pct // 10)

    await message.answer(
        f"📊 <b>Статистика за 7 днів:</b>\n\n"
        f"{bar} <b>{pct}%</b>\n\n"
        f"✅ Прийнято: {taken}\n"
        f"❌ Пропущено: {missed}\n"
        f"📋 Всього: {total}\n\n"
        f"<b>По кожному ліку:</b>\n{breakdown or '  —'}",
        parse_mode="HTML",
    )


# ════════════════════════════════════════════════════════════
#  📖 ІСТОРІЯ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "📖 Історія")
@dp.message(Command("history"))
async def cmd_history(message: types.Message):
    async with pool.acquire() as c:
        logs = await c.fetch(
            """SELECT l.event, l.time, l.slot, p.name
               FROM logs l LEFT JOIN pills p ON l.pill_id = p.id
               WHERE l.user_id=$1 ORDER BY l.time DESC LIMIT 15""",
            message.chat.id,
        )
    if not logs:
        return await message.answer("📭 Історія порожня.")

    ev_names = {
        "open":   "🔓 Відкрито",
        "taken":  "✅ Прийнято",
        "remind": "⏰ Нагадування",
        "missed": "❌ Пропущено",
    }
    text = "📖 <b>Останні події:</b>\n\n"
    for l in logs:
        t    = l["time"].strftime("%d.%m %H:%M")
        name = l["name"] or "—"
        text += f"🔹 {t} — {ev_names.get(l['event'], l['event'])}\n   {name}\n"

    await message.answer(text, parse_mode="HTML")


# ════════════════════════════════════════════════════════════
#  🔥 СЕРІЯ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "🔥 Серія")
@dp.message(Command("streak"))
async def cmd_streak(message: types.Message):
    async with pool.acquire() as c:
        rows = await c.fetch(
            "SELECT DATE(time) as day, event FROM logs "
            "WHERE user_id=$1 ORDER BY time DESC",
            message.chat.id,
        )

    streak   = 0
    days_ok  = set()
    days_bad = set()
    for r in rows:
        day = r["day"]
        if day in days_bad:
            break
        if r["event"] == "missed":
            days_bad.add(day)
            if day not in days_ok:
                break
        elif r["event"] == "taken":
            days_ok.add(day)
            streak += 1

    if streak == 0:
        msg = "😔 Серія ще не почалася. Починаємо з сьогодні!"
    elif streak < 3:
        msg = f"🌱 Серія: <b>{streak} дн.</b> — гарний початок!"
    elif streak < 7:
        msg = f"💪 Серія: <b>{streak} дн.</b> — так тримати!"
    elif streak < 14:
        msg = f"🔥 Серія: <b>{streak} дн.</b> підряд — відмінно!"
    else:
        msg = f"🏆 Серія: <b>{streak} дн.</b> підряд — ви чемпіон!"

    await message.answer(msg, parse_mode="HTML")


# ════════════════════════════════════════════════════════════
#  🏪 АПТЕКА ПОРУЧ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "🏪 Аптека поруч")
@dp.message(Command("find_pharmacy"))
async def cmd_find_pharmacy(message: types.Message):
    await message.answer(
        "📍 Надішліть вашу геолокацію — знайдемо найближчі аптеки:",
        reply_markup=location_kb(),
    )


@dp.message(F.location)
async def handle_location(message: types.Message):
    lat, lon = message.location.latitude, message.location.longitude
    await message.answer("🔍 Шукаємо аптеки...", reply_markup=main_kb())

    query = (
        f"[out:json];"
        f'node["amenity"="pharmacy"](around:2000,{lat},{lon});'
        f"out body;"
    )
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://overpass-api.de/api/interpreter",
                data={"data": query},
                timeout=aiohttp.ClientTimeout(total=12),
            ) as resp:
                data = await resp.json()

        nodes = data.get("elements", [])
        if not nodes:
            return await message.answer("😔 Аптек у радіусі 2 км не знайдено.")

        nodes_dist = sorted(
            [
                (
                    haversine(lat, lon, n["lat"], n["lon"]),
                    n.get("tags", {}).get("name", "Аптека"),
                    n["lat"], n["lon"],
                )
                for n in nodes
            ]
        )

        text = "🏪 <b>Найближчі аптеки:</b>\n\n"
        for i, (dist, name, nlat, nlon) in enumerate(nodes_dist[:5], 1):
            dist_str = f"{int(dist)} м" if dist < 1000 else f"{dist / 1000:.1f} км"
            maps_url = f"https://maps.google.com/?q={nlat},{nlon}"
            text += f"{i}. <b>{name}</b> — {dist_str}\n   <a href='{maps_url}'>📍 Відкрити на карті</a>\n\n"

        await message.answer(text, parse_mode="HTML", disable_web_page_preview=True)

    except Exception as e:
        await message.answer(f"⚠️ Не вдалося отримати дані. Спробуйте пізніше.\n<code>{e}</code>",
                             parse_mode="HTML")


@dp.message(F.text == "⬅️ Назад")
async def back_to_main(message: types.Message):
    await message.answer("Головне меню:", reply_markup=main_kb())


# ════════════════════════════════════════════════════════════
#  👨‍👩‍👧 РОДИЧІ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "👨‍👩‍👧 Родичі")
@dp.message(Command("invite_relative"))
async def cmd_relatives(message: types.Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Запросити (спостерігач)", callback_data="inv_viewer")],
            [InlineKeyboardButton(text="➕ Запросити (адмін)",       callback_data="inv_admin")],
            [InlineKeyboardButton(text="📋 Список підключених",      callback_data="list_rels")],
        ]
    )
    await message.answer("👨‍👩‍👧 Управління родичами:", reply_markup=kb)


@dp.callback_query(F.data.startswith("inv_"))
async def invite_cb(callback: types.CallbackQuery):
    role    = callback.data.split("_")[1]
    me      = await bot.get_me()
    link    = f"https://t.me/{me.username}?start=patient_{callback.from_user.id}_{role}"
    role_ua = "адміна" if role == "admin" else "спостерігача"
    await callback.message.answer(
        f"Надішліть це посилання родичу ({role_ua}):\n\n{link}\n\n"
        f"<b>Спостерігач</b> — бачить статус прийому\n"
        f"<b>Адмін</b> — отримує тривоги і всю статистику",
        parse_mode="HTML",
    )
    await callback.answer()


@dp.callback_query(F.data == "list_rels")
async def list_rels_cb(callback: types.CallbackQuery):
    rels = await get_relatives(callback.from_user.id)
    if not rels:
        await callback.message.answer("📭 Родичів немає.")
        await callback.answer()
        return

    text    = "👨‍👩‍👧 <b>Підключені родичі:</b>\n\n"
    kb_rows = []
    for r in rels:
        try:
            chat = await bot.get_chat(r["relative_id"])
            name = chat.first_name or str(r["relative_id"])
        except Exception:
            name = str(r["relative_id"])
        role_ua = "адмін" if r["role"] == "admin" else "спостерігач"
        text += f"👤 {name} — {role_ua}\n"
        kb_rows.append([InlineKeyboardButton(
            text=f"🗑 Видалити {name}",
            callback_data=f"rrem_{r['relative_id']}",
        )])

    await callback.message.answer(
        text, parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("rrem_"))
async def remove_relative_cb(callback: types.CallbackQuery):
    rel_id = int(callback.data.split("_")[1])
    async with pool.acquire() as c:
        await c.execute(
            "DELETE FROM relatives WHERE relative_id=$1 AND patient_id=$2",
            rel_id, callback.from_user.id,
        )
    await callback.message.answer("✅ Родич видалений.", reply_markup=main_kb())
    await callback.answer()


# ════════════════════════════════════════════════════════════
#  ⚙️ НАЛАШТУВАННЯ
# ════════════════════════════════════════════════════════════
@dp.message(F.text == "⚙️ Налаштування")
async def cmd_settings(message: types.Message, state: FSMContext):
    user   = await get_user(message.chat.id)
    device = user["device_id"] if user and user["device_id"] else "не підключено"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔌 Змінити пристрій", callback_data="change_dev")],
            [InlineKeyboardButton(text="➕ Додати ліки",       callback_data="go_add")],
        ]
    )
    await message.answer(
        f"⚙️ <b>Налаштування</b>\n\n"
        f"Ваш ID: <code>{message.chat.id}</code>\n"
        f"Пристрій: <code>{device}</code>",
        parse_mode="HTML", reply_markup=kb,
    )


@dp.callback_query(F.data == "change_dev")
async def change_device_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(LinkDevice.waiting_id)
    await callback.message.answer("Введіть новий ID пристрою:", reply_markup=cancel_kb())
    await callback.answer()


@dp.callback_query(F.data == "go_add")
async def go_add_pill_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.set_state(AddPill.name)
    await callback.message.answer("💊 Назва ліку:", reply_markup=cancel_kb())
    await callback.answer()


# ════════════════════════════════════════════════════════════
#  FASTAPI APP + ROUTES
# ════════════════════════════════════════════════════════════
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=2, max_size=10)
    await init_db()
    scheduler.add_job(daily_stock_check, "cron", hour=9, minute=0)
    scheduler.add_job(sunday_refill_reminder, "cron", day_of_week="sun", hour=21, minute=0)
    scheduler.start()
    asyncio.create_task(dp.start_polling(bot))
    yield
    scheduler.shutdown()
    await pool.close()


app = FastAPI(lifespan=lifespan)

@app.get("/")
async def root():
    return {"message": "💊 Сервер Pill Box успішно працює!"}

@app.get("/api/log")
async def log_from_esp(
    user_id: str = Q(...),
    event:   str = Q(...),
    slot:    int = Q(0),
    secret:  str = Q(""),
):
    """Called by ESP32 on every pill event."""
    if secret != API_SECRET:
        raise HTTPException(403, "Forbidden")

    uid      = int(user_id)
    time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")

    async with pool.acquire() as c:
        pill = await c.fetchrow(
            "SELECT * FROM pills WHERE user_id=$1 AND slot=$2", uid, slot
        )
        await c.execute(
            "INSERT INTO logs(user_id, pill_id, slot, event) VALUES($1,$2,$3,$4)",
            uid, pill["id"] if pill else None, slot, event,
        )
        remaining = None
        if event == "taken" and pill:
            await c.execute(
                "UPDATE pills SET remaining_count = GREATEST(0, remaining_count - 1) WHERE id=$1",
                pill["id"],
            )
            remaining = await c.fetchval(
                "SELECT remaining_count FROM pills WHERE id=$1", pill["id"]
            )

    pill_name = pill["name"] if pill else f"Комірка {slot}"

    try:
        if event == "open":
            await bot.send_message(
                uid,
                f"💊 Час прийняти <b>{pill_name}</b>!\n🕐 {time_str}",
                parse_mode="HTML",
            )

        elif event == "taken":
            await notify_relatives(
                uid, f"✅ Пацієнт прийняв <b>{pill_name}</b> о {time_str}", "viewer"
            )
            
            if remaining is not None:
                if remaining > 0:
                    await bot.send_message(
                        uid,
                        f"✅ <b>{pill_name}</b> прийнято о {time_str}\n"
                        f"📥 <b>Комірка {slot} тепер порожня!</b> Покладіть туди 1 таблетку з упаковки.\n"
                        f"📦 Залишок в упаковці: {remaining} шт.",
                        parse_mode="HTML",
                    )
                    
                    if remaining <= 3:
                        await bot.send_message(
                            uid,
                            f"⚠️ <b>Залишилось мало ліків в упаковці!</b>\n"
                            f"Натисніть 🏪 Аптека поруч щоб знайти де купити.",
                            parse_mode="HTML",
                        )
                else:
                    await bot.send_message(
                        uid,
                        f"✅ <b>{pill_name}</b> прийнято о {time_str}\n"
                        f"🚨 <b>Упаковка повністю порожня!</b> Комірка {slot} залишилася без ліків.\n"
                        f"Натисніть 🏪 Аптека поруч, щоб купити нові ліки.",
                        parse_mode="HTML",
                    )

        elif event == "remind":
            await bot.send_message(
                uid,
                f"⏰ Нагадування: прийміть <b>{pill_name}</b>!",
                parse_mode="HTML",
            )
            await notify_relatives(
                uid, f"⏰ Пацієнт ще не прийняв <b>{pill_name}</b>!", "admin"
            )

        elif event == "missed":
            await bot.send_message(
                uid,
                f"❌ <b>{pill_name}</b> — прийом пропущено!",
                parse_mode="HTML",
            )
            await notify_relatives(
                uid,
                f"🚨 ТРИВОГА! Пацієнт пропустив прийом <b>{pill_name}</b>!",
                "viewer",
            )

    except Exception as e:
        print(f"[Telegram error] {e}")

    return {"status": "ok"}


@app.get("/api/schedule")
async def get_schedule(device_id: str = Q(...), secret: str = Q("")):
    """
    ESP32 calls this on boot to get its full schedule.
    Returns JSON ready to parse in Arduino.
    """
    if secret != API_SECRET:
        raise HTTPException(403, "Forbidden")

    async with pool.acquire() as c:
        user = await c.fetchrow(
            "SELECT * FROM users WHERE device_id=$1", device_id
        )
        if not user:
            raise HTTPException(404, "Device not found")

        pills = await c.fetch(
            "SELECT * FROM pills WHERE user_id=$1", user["telegram_id"]
        )
        schedule = []
        for p in pills:
            s = await c.fetchrow("SELECT * FROM schedule WHERE pill_id=$1", p["id"])
            if s:
                schedule.append({
                    "slot":   p["slot"],
                    "name":   p["name"],
                    "dosage": p["dosage"],
                    "times":  json.loads(s["times"]),
                    "days":   json.loads(s["days"]),
                })

    return {
        "user_id":  str(user["telegram_id"]),
        "schedule": schedule,
    }



# ════════════════════════════════════════════════════════════
#  SCHEDULED JOB — щоденна перевірка залишків
# ════════════════════════════════════════════════════════════
async def daily_stock_check():
    async with pool.acquire() as c:
        low = await c.fetch(
            """SELECT p.*, u.telegram_id
               FROM pills p JOIN users u ON p.user_id = u.telegram_id
               WHERE p.remaining_count <= 3 AND p.total_count > 0"""
        )
    for p in low:
        try:
            await bot.send_message(
                p["telegram_id"],
                f"⚠️ <b>Закінчуються ліки!</b>\n\n"
                f"💊 {p['name']}: {p['remaining_count']} шт.\n\n"
                f"Натисніть 🏪 Аптека поруч або /refill після поповнення.",
                parse_mode="HTML",
            )
        except Exception:
            pass


async def sunday_refill_reminder():
    """Нагадує всім користувачам у неділю ввечері заповнити аптечку."""
    async with pool.acquire() as c:
        users = await c.fetch("SELECT telegram_id FROM users")
        for u in users:
            try:
                await bot.send_message(
                    u["telegram_id"],
                    "🔔 <b>Тиждень завершується!</b>\n"
                    "Час заповнити аптечку на наступні 7 днів. Натисніть «📝 Інструкція на тиждень», щоб отримати актуальну схему розкладки.",
                    parse_mode="HTML"
                )
            except Exception:
                pass


# ════════════════════════════════════════════════════════════
#  ENTRY POINT
# ════════════════════════════════════════════════════════════
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)