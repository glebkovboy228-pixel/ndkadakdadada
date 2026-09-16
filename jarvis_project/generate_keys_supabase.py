"""
Генератор лицензионных ключей для Supabase.
Создаёт ключи и загружает их в базу.
Запуск: python generate_keys_supabase.py
"""

import secrets
import string
import hashlib
import requests

# === ТВОИ ДАННЫЕ SUPABASE ===
SUPABASE_URL = "https://pvfzksylzxpxpktymgem.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InB2Znprc3lsenhweHBrdHltZ2VtIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1ODk0NjMsImV4cCI6MjEwNTE2NTQ2M30.atnj653ishrGqKhtKDXQ0jiDFmTxFXTRFTDayT06yBs"
SECRET_SALT = "wawswdwdwswa"
# ============================


def generate_key(length=25):
    """Создаёт ключ вида: XXXXX-XXXXX-XXXXX-XXXXX-XXXXX"""
    alphabet = string.ascii_uppercase + string.digits
    raw = ''.join(secrets.choice(alphabet) for _ in range(length))
    parts = [raw[i:i+5] for i in range(0, length, 5)]
    return '-'.join(parts)


def hash_key(key):
    """Хэширует ключ с солью — в базе хранятся только хэши."""
    return hashlib.sha256((key + SECRET_SALT).encode()).hexdigest()


def upload_keys_to_supabase(keys):
    """Загружает ключи (в виде хэшей) в Supabase."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }

    # Готовим данные — только хэши, не сами ключи
    rows = [{"key_hash": hash_key(k), "status": "unused"} for k in keys]

    # Загружаем партиями по 500
    batch_size = 500
    total = 0

    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/license_keys",
            json=batch,
            headers=headers,
        )
        if r.status_code == 201:
            total += len(batch)
            print(f"Загружено: {total}/{len(rows)}")
        else:
            print(f"Ошибка при загрузке: {r.status_code} {r.text}")
            break

    return total


def export_keys_txt(keys, filename="keys_for_sale.txt"):
    """Сохраняет ключи в файл для продажи."""
    with open(filename, "w", encoding="utf-8") as f:
        for k in keys:
            f.write(k + "\n")
    print(f"Ключи сохранены в {filename}")


def get_stats():
    """Показывает статистику по ключам в базе."""
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
    }
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/license_keys?select=status",
        headers=headers,
    )
    if r.status_code == 200:
        data = r.json()
        unused = sum(1 for d in data if d["status"] == "unused")
        activated = sum(1 for d in data if d["status"] == "activated")
        print(f"\n=== Статистика ===")
        print(f"  Не использовано: {unused}")
        print(f"  Активировано: {activated}")
        print(f"  Всего: {len(data)}")
    else:
        print(f"Ошибка: {r.status_code}")


if __name__ == "__main__":
    print("=== ГЕНЕРАТОР КЛЮЧЕЙ + SUPABASE ===\n")

    choice = input(
        "1 — Сгенерировать и загрузить ключи\n"
        "2 — Показать статистику\n"
        "Выбор: "
    ).strip()

    if choice == "1":
        count = int(input("Сколько ключей? (по умолчанию 10000): ") or "10000")
        print(f"\nГенерирую {count} ключей...")

        keys = []
        seen = set()
        while len(keys) < count:
            k = generate_key(25)
            if k not in seen:
                seen.add(k)
                keys.append(k)

        print(f"Сгенерировано: {len(keys)}")

        export_keys_txt(keys)
        print("\nЗагружаю в Supabase...")
        uploaded = upload_keys_to_supabase(keys)
        print(f"Загружено в базу: {uploaded}")

    elif choice == "2":
        get_stats()
