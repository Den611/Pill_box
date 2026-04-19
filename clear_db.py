import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DB_URL = os.getenv("DB_URL")

if not DB_URL:
    print("❌ Помилка: Не знайдено DB_URL у файлі .env!")
    exit()


def clear_database():
    print("⏳ Підключення до бази даних...")
    conn = None
    cursor = None
    try:
        conn = psycopg2.connect(DB_URL)
        cursor = conn.cursor()

        print("🧹 Видалення таблиць...")
        cursor.execute("DROP TABLE IF EXISTS logs CASCADE;")
        cursor.execute("DROP TABLE IF EXISTS schedule CASCADE;")
        cursor.execute("DROP TABLE IF EXISTS pills CASCADE;")
        cursor.execute("DROP TABLE IF EXISTS relatives CASCADE;")

        conn.commit()
        print("✅ Готово! Таблиці будуть створені наново при запуску бота.")

    except Exception as e:
        print(f"⚠️ Помилка: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    confirm = input("⚠️ Видалити ВСІ дані? (y/n): ")
    if confirm.lower() == 'y':
        clear_database()
    else:
        print("🛑 Скасовано.")