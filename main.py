import os
import asyncio
import psycopg2
import uvicorn
from datetime import datetime, timedelta
from contextlib import asynccontextmanager
from dotenv import load_dotenv
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- НАЛАШТУВАННЯ ---
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
DB_URL = os.getenv("DB_URL")

if not BOT_TOKEN or not DB_URL:
    print("❌ Помилка: Не знайдено BOT_TOKEN або DB_URL у файлі .env!")
    exit()

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
scheduler = AsyncIOScheduler()

# Глобальний стан для ESP (шматок логіки для мікроконтролера)
global_esp_states = {}


def get_user_esp_state(user_id: int) -> dict:
    if user_id not in global_esp_states:
        global_esp_states[user_id] = {"slot_1": 0, "slot_2": 0, "alert": 0}
    return global_esp_states[user_id]


# --- СТАНИ FSM ---
class AddPill(StatesGroup):
    waiting_for_name = State()
    waiting_for_slot = State()
    waiting_for_time = State()


class EditPill(StatesGroup):
    waiting_for_new_time = State()


# --- КЛАВІАТУРИ ---
def get_main_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💊 Мої ліки"), KeyboardButton(text="➕ Додати ліки")],
            [KeyboardButton(text="👨‍👩‍👧‍👦 Підключити родича")]
        ],
        resize_keyboard=True
    )


def get_pill_inline_kb(pill_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✏️ Змінити час", callback_data=f"edit_{pill_id}"),
                InlineKeyboardButton(text="❌ Видалити", callback_data=f"del_{pill_id}")
            ]
        ]
    )


# --- РОБОТА З БАЗОЮ ДАНИХ ---
def get_db_connection():
    return psycopg2.connect(DB_URL)


def execute_query(query: str, params: tuple = None, fetchone=False, fetchall=False, commit=False):
    conn = None
    cursor = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(query, params or ())

        result = None
        if fetchone:
            result = cursor.fetchone()
        elif fetchall:
            result = cursor.fetchall()

        if commit:
            conn.commit()

        return result
    except Exception as e:
        print(f"⚠️ Помилка бази даних: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


def init_db():
    execute_query('''
        CREATE TABLE IF NOT EXISTS Pills (
            id SERIAL PRIMARY KEY, 
            user_id BIGINT, 
            name TEXT, 
            slot_number INTEGER
        )''', commit=True)

    execute_query('''
        CREATE TABLE IF NOT EXISTS Schedule (
            id SERIAL PRIMARY KEY, 
            pill_id INTEGER REFERENCES Pills(id) ON DELETE CASCADE, 
            time_to_take TEXT, 
            status TEXT
        )''', commit=True)

    execute_query('''
        CREATE TABLE IF NOT EXISTS Relatives (
            id SERIAL PRIMARY KEY,
            relative_chat_id BIGINT,
            patient_chat_id BIGINT,
            UNIQUE(relative_chat_id, patient_chat_id)
        )''', commit=True)
    print("✅ База даних успішно ініціалізована.")


# --- ФОНОВІ ЗАВДАННЯ (АЛЕРТИ ТА ПЕРЕВІРКИ) ---
async def reset_daily_statuses():
    execute_query("UPDATE Schedule SET status = 'очікується'", commit=True)
    print("🔄 Статуси ліків скинуто на 'очікується'.")


async def check_schedule():
    current_time = datetime.now().strftime("%H:%M")
    query = '''
        SELECT Schedule.id, Pills.name, Pills.slot_number, Pills.user_id 
        FROM Schedule 
        JOIN Pills ON Schedule.pill_id = Pills.id 
        WHERE Schedule.time_to_take = %s AND Schedule.status = 'очікується'
    '''
    tasks = execute_query(query, (current_time,), fetchall=True)

    if not tasks:
        return

    for task in tasks:
        sched_id, pill_name, slot_num, user_id = task
        state = get_user_esp_state(user_id)

        # Сигнал для ESP на автоматичне відкриття комірки
        state[f"slot_{slot_num}"] = 1

        try:
            await bot.send_message(user_id,
                                   f"💊 Час приймати ліки: {pill_name}!\nКомірка №{slot_num} автоматично відчиняється.")
        except Exception as e:
            print(f"Не вдалося відправити повідомлення користувачу {user_id}: {e}")

        execute_query("UPDATE Schedule SET status = 'надіслано' WHERE id = %s", (sched_id,), commit=True)

        # Плануємо перевірку через 10 хвилин, чи закрив пацієнт комірку (прийняв ліки)
        scheduler.add_job(
            check_if_taken,
            'date',
            run_date=datetime.now() + timedelta(minutes=10),
            args=[sched_id, pill_name, user_id]
        )


async def check_if_taken(sched_id: int, pill_name: str, user_id: int):
    result = execute_query("SELECT status FROM Schedule WHERE id = %s", (sched_id,), fetchone=True)

    if result and result[0] != 'прийнято':
        state = get_user_esp_state(user_id)
        state["alert"] = 1

        # Сповіщення пацієнту
        for _ in range(3):
            try:
                await bot.send_message(user_id, f"🚨 АЛЯРМ! Ви забули випити {pill_name}!")
            except Exception:
                pass
            await asyncio.sleep(2)

        # Сповіщення родичам конкретного пацієнта
        query = "SELECT relative_chat_id FROM Relatives WHERE patient_chat_id = %s"
        relatives = execute_query(query, (user_id,), fetchall=True)

        if relatives:
            for rel in relatives:
                try:
                    await bot.send_message(rel[0], f"⚠️ УВАГА! Пацієнт пропустив прийом ліків: {pill_name}!")
                except Exception:
                    pass


# --- API (FastAPI) ДЛЯ РОБОТИ З ESP ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    scheduler.add_job(check_schedule, 'cron', minute='*')
    scheduler.add_job(reset_daily_statuses, 'cron', hour=0, minute=0)
    scheduler.start()
    asyncio.create_task(dp.start_polling(bot))
    yield


app = FastAPI(lifespan=lifespan)

@app.get("/")
def read_root():
    return {"message": "Сервер розумної аптечки працює і готовий до роботи! 💊"}

@app.get("/api/time")
def get_current_time():
    # Отримуємо поточний час
    now = datetime.now()
    return {
        "hour": now.hour,
        "minute": now.minute,
        "formatted": now.strftime("%H:%M") # Наприклад: "14:30"
    }

@app.get("/api/state/{user_id}")
def get_state(user_id: int):
    # ESP викликає цей метод кожні кілька секунд. Якщо slot_X == 1, сервопривід відчиняє кришку.
    return get_user_esp_state(user_id)


@app.get("/api/taken/{user_id}/{slot}")
async def pill_taken(user_id: int, slot: int):
    # ESP викликає цей метод, коли датчик фіксує, що комірка закрилася або ліки взято.
    state = get_user_esp_state(user_id)
    state[f"slot_{slot}"] = 0
    state["alert"] = 0

    query = '''
        UPDATE Schedule SET status = 'прийнято' 
        WHERE id IN (
            SELECT Schedule.id FROM Schedule 
            JOIN Pills ON Schedule.pill_id = Pills.id 
            WHERE Pills.slot_number = %s AND Pills.user_id = %s AND Schedule.status = 'надіслано'
        )
    '''
    execute_query(query, (slot, user_id), commit=True)

    try:
        await bot.send_message(user_id, f"✅ Підтверджено: ліки з комірки {slot} прийнято!", reply_markup=get_main_kb())
    except Exception as e:
        print(f"Помилка відправки підтвердження: {e}")

    return {"status": "success"}


# --- ТЕЛЕГРАМ БОТ (ХЕНДЛЕРИ) ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()

    # Перевірка, чи перейшов користувач за посиланням-запрошенням для родича
    if len(args) > 1 and args[1].startswith("patient_"):
        try:
            patient_id = int(args[1].split("_")[1])
            relative_id = message.chat.id

            if patient_id == relative_id:
                return await message.answer("❌ Ви не можете підписатися самі на себе.", reply_markup=get_main_kb())

            query = "INSERT INTO Relatives (relative_chat_id, patient_chat_id) VALUES (%s, %s) ON CONFLICT DO NOTHING"
            execute_query(query, (relative_id, patient_id), commit=True)

            await message.answer(
                "✅ Ви успішно підписалися на сповіщення пацієнта! Якщо він забуде випити ліки, я вам напишу.",
                reply_markup=get_main_kb())

            try:
                await bot.send_message(patient_id, "👨‍👩‍👧‍👦 Ваш родич щойно успішно підключився до ваших сповіщень!")
            except Exception:
                pass
            return

        except Exception as e:
            print(f"Помилка підключення родича: {e}")

    # Звичайний запуск бота
    text = f"Привіт! Я Smart Pillbox 💊\nВаш ID: {message.chat.id}"
    await message.answer(text, reply_markup=get_main_kb())


@dp.message(F.text == "👨‍👩‍👧‍👦 Підключити родича")
async def generate_relative_link(message: types.Message):
    bot_info = await bot.get_me()
    link = f"https://t.me/{bot_info.username}?start=patient_{message.chat.id}"

    text = (
        "🔗 Надішліть це посилання вашому родичу:\n\n"
        f"{link}\n\n"
        "Коли він/вона перейде за ним і натисне 'Розпочати', то автоматично підпишеться на сповіщення, якщо ви забудете випити ліки."
    )
    await message.answer(text)


@dp.message(F.text == "💊 Мої ліки")
async def view_pills(message: types.Message):
    query = '''
        SELECT Pills.id, Pills.name, Pills.slot_number, Schedule.time_to_take, Schedule.status 
        FROM Pills 
        LEFT JOIN Schedule ON Pills.id = Schedule.pill_id
        WHERE Pills.user_id = %s ORDER BY Schedule.time_to_take
    '''
    pills = execute_query(query, (message.chat.id,), fetchall=True)

    if not pills:
        await message.answer("Ваша аптечка порожня.", reply_markup=get_main_kb())
        return

    await message.answer("📋 Ваш розклад:", reply_markup=get_main_kb())
    for p in pills:
        pill_id, name, slot, t, s = p
        status_icon = "✅" if s == "прийнято" else "⏳" if s == "очікується" else "🚨"
        text = f"🔸 {name} (Комірка {slot})\n⏰ Час: {t} | Статус: {s} {status_icon}"
        await message.answer(text, reply_markup=get_pill_inline_kb(pill_id))


@dp.callback_query(F.data.startswith("del_"))
async def delete_pill(callback: types.CallbackQuery):
    pill_id = int(callback.data.split("_")[1])
    execute_query("DELETE FROM Pills WHERE id = %s AND user_id = %s", (pill_id, callback.message.chat.id), commit=True)

    await callback.message.delete()
    await callback.answer("✅ Ліки видалено")


@dp.callback_query(F.data.startswith("edit_"))
async def edit_pill(callback: types.CallbackQuery, state: FSMContext):
    pill_id = int(callback.data.split("_")[1])
    await state.update_data(edit_pill_id=pill_id)
    await callback.message.answer("Введіть новий час у форматі ГГ:ХХ (наприклад, 08:30):")
    await state.set_state(EditPill.waiting_for_new_time)
    await callback.answer()


@dp.message(EditPill.waiting_for_new_time)
async def save_new_time(message: types.Message, state: FSMContext):
    t = message.text.strip()

    if len(t) == 4 and ":" in t:
        t = "0" + t

    if len(t) != 5 or ":" not in t:
        await message.answer("⚠️ Помилка формату! Будь ласка, введіть час у форматі ГГ:ХХ.")
        return

    data = await state.get_data()
    execute_query("UPDATE Schedule SET time_to_take = %s, status = 'очікується' WHERE pill_id = %s",
                  (t, data['edit_pill_id']), commit=True)

    await state.clear()
    await message.answer("✅ Час прийому успішно змінено!", reply_markup=get_main_kb())


@dp.message(F.text == "➕ Додати ліки")
async def cmd_add(message: types.Message, state: FSMContext):
    await message.answer("Введіть назву ліків:", reply_markup=types.ReplyKeyboardRemove())
    await state.set_state(AddPill.waiting_for_name)


@dp.message(AddPill.waiting_for_name)
async def process_name(message: types.Message, state: FSMContext):
    if message.text in ["💊 Мої ліки", "➕ Додати ліки", "👨‍👩‍👧‍👦 Підключити родича"]:
        await state.clear()
        return await message.answer("Додавання скасовано.", reply_markup=get_main_kb())

    await state.update_data(pill_name=message.text.strip())
    await message.answer("Введіть номер комірки розумної аптечки (наприклад, 1 або 2):")
    await state.set_state(AddPill.waiting_for_slot)


@dp.message(AddPill.waiting_for_slot)
async def process_slot(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        return await message.answer("⚠️ Будь ласка, введіть число (номер комірки).")

    await state.update_data(slot_number=int(message.text))
    await message.answer("Введіть час прийому у форматі ГГ:ХХ (наприклад, 09:00):")
    await state.set_state(AddPill.waiting_for_time)


@dp.message(AddPill.waiting_for_time)
async def process_time(message: types.Message, state: FSMContext):
    t = message.text.strip()

    if len(t) == 4 and ":" in t:
        t = "0" + t

    if len(t) != 5 or ":" not in t:
        return await message.answer("⚠️ Помилка формату! Введіть час у форматі ГГ:ХХ.")

    data = await state.get_data()

    pill_query = "INSERT INTO Pills (user_id, name, slot_number) VALUES (%s, %s, %s) RETURNING id"
    pill_id = \
    execute_query(pill_query, (message.chat.id, data['pill_name'], data['slot_number']), fetchone=True, commit=True)[0]

    schedule_query = "INSERT INTO Schedule (pill_id, time_to_take, status) VALUES (%s, %s, 'очікується')"
    execute_query(schedule_query, (pill_id, t), commit=True)

    await state.clear()
    await message.answer(f"✅ Ліки {data['pill_name']} успішно додано!", reply_markup=get_main_kb())


# --- ТОЧКА ВХОДУ ---
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)