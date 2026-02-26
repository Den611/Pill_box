import os
import asyncio
import psycopg2
import uvicorn
from dotenv import load_dotenv  # Додано
from fastapi import FastAPI
from contextlib import asynccontextmanager
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from datetime import datetime, timedelta

# Завантажуємо змінні з файлу .env
load_dotenv()

# --- НАЛАШТУВАННЯ (Беремо з оточення) ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DB_URL")

# Перевірка на всяк випадок
if not BOT_TOKEN or not DB_URL:
    print("❌ Помилка: Не знайдено BOT_TOKEN або DB_URL у файлі .env!")
    exit()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# --- РЕШТА КОДУ (БЕЗ ЗМІН) ---

global_esp_states = {}

def get_user_esp_state(user_id):
    if user_id not in global_esp_states:
        global_esp_states[user_id] = {"slot_1": 0, "slot_2": 0, "alert": 0}
    return global_esp_states[user_id]

class AddPill(StatesGroup):
    waiting_for_name = State()
    waiting_for_slot = State()
    waiting_for_time = State()

class EditPill(StatesGroup):
    waiting_for_new_time = State()

def get_main_kb():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💊 Мої ліки"), KeyboardButton(text="➕ Додати ліки")],
            [KeyboardButton(text="👨‍👩‍👧‍👦 Підписатись як родич")]
        ],
        resize_keyboard=True
    )

def get_pill_inline_kb(pill_id):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Змінити час", callback_data=f"edit_{pill_id}"),
                InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_{pill_id}")
            ]
        ]
    )

def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS Pills 
                 (id SERIAL PRIMARY KEY, user_id BIGINT, name TEXT, slot_number INTEGER)''')
    c.execute('''CREATE TABLE IF NOT EXISTS Schedule 
                 (id SERIAL PRIMARY KEY, pill_id INTEGER REFERENCES Pills(id) ON DELETE CASCADE, time_to_take TEXT, status TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS Relatives 
                 (chat_id BIGINT PRIMARY KEY)''')
    conn.commit()
    c.close()
    conn.close()

async def reset_daily_statuses():
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE Schedule SET status = 'очікується'")
    conn.commit()
    c.close()
    conn.close()

async def start_bot():
    await dp.start_polling(bot)

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(check_schedule, 'cron', minute='*')
    scheduler.add_job(reset_daily_statuses, 'cron', hour=0, minute=0)
    scheduler.start()
    asyncio.create_task(start_bot())
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/api/state/{user_id}")
def get_state(user_id: int):
    return get_user_esp_state(user_id)

@app.get("/api/taken/{user_id}/{slot}")
async def pill_taken(user_id: int, slot: int):
    state = get_user_esp_state(user_id)
    state[f"slot_{slot}"] = 0
    state["alert"] = 0
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''UPDATE Schedule SET status = 'прийнято' 
                 WHERE id IN (
                     SELECT Schedule.id FROM Schedule 
                     JOIN Pills ON Schedule.pill_id = Pills.id 
                     WHERE Pills.slot_number = %s AND Pills.user_id = %s AND Schedule.status = 'надіслано'
                 )''', (slot, user_id))
    conn.commit()
    c.close()
    conn.close()
    await bot.send_message(user_id, f"✅ Підтверджено: ліки з комірки {slot} прийнято!", reply_markup=get_main_kb())
    return {"status": "success"}

async def check_schedule():
    current_time = datetime.now().strftime("%H:%M")
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT Schedule.id, Pills.name, Pills.slot_number, Pills.user_id 
                 FROM Schedule JOIN Pills ON Schedule.pill_id = Pills.id 
                 WHERE Schedule.time_to_take = %s AND Schedule.status = 'очікується' ''', (current_time,))
    tasks = c.fetchall()
    for task in tasks:
        sched_id, pill_name, slot_num, user_id = task
        state = get_user_esp_state(user_id)
        state[f"slot_{slot_num}"] = 1
        await bot.send_message(user_id, f"💊 Час приймати ліки: **{pill_name}**!\nВідкрий комірку №{slot_num}.")
        c.execute("UPDATE Schedule SET status = 'надіслано' WHERE id = %s", (sched_id,))
        scheduler.add_job(check_if_taken, 'date', run_date=datetime.now() + timedelta(minutes=10), args=[sched_id, pill_name, user_id])
    conn.commit()
    c.close()
    conn.close()

async def check_if_taken(sched_id, pill_name, user_id):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT status FROM Schedule WHERE id = %s", (sched_id,))
    result = c.fetchone()
    if result and result[0] != 'прийнято':
        state = get_user_esp_state(user_id)
        state["alert"] = 1
        for i in range(3):
            await bot.send_message(user_id, f"🚨 АЛЕРТ! Ви забули випити **{pill_name}**!")
            await asyncio.sleep(2)
        c.execute("SELECT chat_id FROM Relatives")
        relatives = c.fetchall()
        for rel in relatives:
            try:
                await bot.send_message(rel[0], f"⚠️ УВАГА! Пацієнт пропустив прийом: **{pill_name}**!")
            except: pass
    c.close()
    conn.close()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(f"Привіт! Я Smart Pillbox 💊\nID: `{message.chat.id}`", reply_markup=get_main_kb(), parse_mode="Markdown")

@dp.message(F.text == "👨‍👩‍👧‍👦 Підписатись як родич")
async def add_relative(message: types.Message):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO Relatives (chat_id) VALUES (%s) ON CONFLICT DO NOTHING", (message.chat.id,))
    conn.commit()
    c.close()
    conn.close()
    await message.answer("✅ Тепер ви отримуватимете екстрені сповіщення.", reply_markup=get_main_kb())

@dp.message(F.text == "💊 Мої ліки")
async def view_pills(message: types.Message):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''SELECT Pills.id, Pills.name, Pills.slot_number, Schedule.time_to_take, Schedule.status 
                 FROM Pills LEFT JOIN Schedule ON Pills.id = Schedule.pill_id
                 WHERE Pills.user_id = %s ORDER BY Schedule.time_to_take''', (message.chat.id,))
    pills = c.fetchall()
    c.close()
    conn.close()
    if not pills:
        await message.answer("Аптечка порожня.", reply_markup=get_main_kb())
        return
    await message.answer("📋 **Ваш розклад:**", reply_markup=get_main_kb())
    for p in pills:
        pill_id, name, slot, t, s = p
        await message.answer(f"🔸 **{name}** (Комірка {slot})\n⏰ Час: {t} | Статус: {s}", reply_markup=get_pill_inline_kb(pill_id))

@dp.callback_query(F.data.startswith("del_"))
async def delete_pill(callback: types.CallbackQuery):
    pill_id = int(callback.data.split("_")[1])
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM Pills WHERE id = %s AND user_id = %s", (pill_id, callback.message.chat.id))
    conn.commit()
    c.close()
    conn.close()
    await callback.message.delete()
    await callback.answer("Видалено")

@dp.callback_query(F.data.startswith("edit_"))
async def edit_pill(callback: types.CallbackQuery, state: FSMContext):
    await state.update_data(edit_pill_id=int(callback.data.split("_")[1]))
    await callback.message.answer("Новий час (ГГ:ХХ):")
    await state.set_state(EditPill.waiting_for_new_time)
    await callback.answer()

@dp.message(EditPill.waiting_for_new_time)
async def save_new_time(message: types.Message, state: FSMContext):
    t = message.text
    if len(t) == 4 and t[1] == ":": t = "0" + t
    if len(t) != 5:
        await message.answer("Помилка формату!")
        return
    data = await state.get_data()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("UPDATE Schedule SET time_to_take = %s, status = 'очікується' WHERE pill_id = %s", (t, data['edit_pill_id']))
    conn.commit()
    c.close()
    conn.close()
    await state.clear()
    await message.answer("✅ Змінено!", reply_markup=get_main_kb())

@dp.message(F.text == "➕ Додати ліки")
async def cmd_add(message: types.Message, state: FSMContext):
    await message.answer("Назва ліків:", reply_markup=get_main_kb())
    await state.set_state(AddPill.waiting_for_name)

@dp.message(AddPill.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    if message.text in ["💊 Мої ліки", "➕ Додати ліки", "👨‍👩‍👧‍👦 Підписатись як родич"]:
        await state.clear()
        return
    await state.update_data(pill_name=message.text)
    await message.answer("Комірка (1/2):")
    await state.set_state(AddPill.waiting_for_slot)

@dp.message(AddPill.waiting_for_slot)
async def process_slot(message: types.Message, state: FSMContext):
    await state.update_data(slot_number=int(message.text))
    await message.answer("Час прийому (ГГ:ХХ):")
    await state.set_state(AddPill.waiting_for_time)

@dp.message(AddPill.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    t = message.text
    if len(t) == 4 and t[1] == ":": t = "0" + t
    data = await state.get_data()
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT INTO Pills (user_id, name, slot_number) VALUES (%s, %s, %s) RETURNING id", (message.chat.id, data['pill_name'], data['slot_number']))
    p_id = c.fetchone()[0]
    c.execute("INSERT INTO Schedule (pill_id, time_to_take, status) VALUES (%s, %s, 'очікується')", (p_id, t))
    conn.commit()
    c.close()
    conn.close()
    await state.clear()
    await message.answer("✅ Додано!", reply_markup=get_main_kb())

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)