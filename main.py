import asyncio
import sqlite3
import uvicorn
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = "8622592417:AAFx0RFEPlAtydcD8hQYtkrxzGfL6X0I1Vs"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def execute_db(query, params=(), fetchone=False, fetchall=False):
    conn = sqlite3.connect("pillbox.db")
    cursor = conn.cursor()
    cursor.execute(query, params)
    res = cursor.fetchone() if fetchone else cursor.fetchall() if fetchall else None
    conn.commit()
    conn.close()
    return res

def init_db():
    execute_db('''CREATE TABLE IF NOT EXISTS Logs (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, event TEXT, time TEXT)''')
    execute_db('''CREATE TABLE IF NOT EXISTS Relatives (id INTEGER PRIMARY KEY AUTOINCREMENT, relative_id TEXT, patient_id TEXT, UNIQUE(relative_id, patient_id))''')

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    asyncio.create_task(dp.start_polling(bot))
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/api/log")
async def log_from_esp(user_id: str, event: str):
    time_str = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
    execute_db("INSERT INTO Logs (user_id, event, time) VALUES (?, ?, ?)", (user_id, event, time_str))
    print(f"\n[{time_str}] 📥 Отримано сигнал від ESP32: {event}")

    relatives = execute_db("SELECT relative_id FROM Relatives WHERE patient_id = ?", (user_id,), fetchall=True)

    try:
        if event == "open":
            await bot.send_message(user_id, f"💊 ЧАС ПРИЙМАТИ ЛІКИ ({time_str})!")
        elif event == "taken":
            await bot.send_message(user_id, "✅ Таблетку взято. Комірку закрито.")
            for rel in relatives:
                await bot.send_message(rel[0], f"✅ Пацієнт прийняв ліки о {time_str}. Все добре.")
        elif event == "remind":
            await bot.send_message(user_id, "⏰ Будь ласка, прийміть ліки!")
            for rel in relatives:
                await bot.send_message(rel[0], f"🚨 ТРИВОГА! Пацієнт ігнорує прийом ліків!")
    except Exception as e:
        print(f"❌ ПОМИЛКА відправки в Телеграм: {e}")

    return {"status": "success"}

def main_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📅 Історія прийомів")],
        [KeyboardButton(text="👨‍👩‍👧‍👦 Підключити родича")]
    ], resize_keyboard=True)

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("patient_"):
        p_id = args[1].split("_")[1]
        execute_db("INSERT OR IGNORE INTO Relatives (relative_id, patient_id) VALUES (?, ?)", (str(message.chat.id), p_id))
        return await message.answer(f"✅ Ви підключені як родич до пацієнта!", reply_markup=main_kb())

    await message.answer(f"Вітаю! Ваш ID: {message.chat.id}\nВикористовуйте меню нижче.", reply_markup=main_kb())

@dp.message(F.text == "📅 Історія прийомів")
async def show_history(message: types.Message):
    logs = execute_db("SELECT event, time FROM Logs WHERE user_id = ? ORDER BY id DESC LIMIT 10", (str(message.chat.id),), fetchall=True)
    if not logs:
        return await message.answer("📭 Історія порожня.")
    names = {"open": "🔓 Відкрито", "taken": "✅ ПРИЙНЯТО", "remind": "⚠️ Пропущено (Нагадування)"}
    text = "📋 **Останні події:**\n\n"
    for log in logs:
        text += f"🔹 {log[1]} — {names.get(log[0], log[0])}\n"
    await message.answer(text)

@dp.message(F.text == "👨‍👩‍👧‍👦 Підключити родича")
async def share_link(message: types.Message):
    me = await bot.get_me()
    link = f"https://t.me/{me.username}?start=patient_{message.chat.id}"
    await message.answer(f"Надішліть це посилання вашим рідним:\n\n{link}")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)