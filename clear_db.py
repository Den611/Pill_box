import os
import psycopg2
from dotenv import load_dotenv

# Завантажуємо змінні з файлу .env
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

        # Видаляємо таблиці. CASCADE гарантує, що зв'язані дані (наприклад, розклад для ліків) теж видаляться.
        print("🧹 Видалення таблиць...")
        cursor.execute("DROP TABLE IF EXISTS Schedule CASCADE;")
        cursor.execute("DROP TABLE IF EXISTS Pills CASCADE;")
        cursor.execute("DROP TABLE IF EXISTS Relatives CASCADE;")

        conn.commit()
        print("✅ Базу даних повністю очищено! Таблиці будуть створені наново при запуску бота.")

    except Exception as e:
        print(f"⚠️ Сталася помилка під час очищення бази: {e}")
        if conn:
            conn.rollback()
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()


if __name__ == "__main__":
    confirm = input("⚠️ ВИ ВПЕВНЕНІ, що хочете видалити ВСІ дані з бази? (y/n): ")
    if confirm.lower() == 'y':
        clear_database()
    else:
        print("🛑 Очищення скасовано.")