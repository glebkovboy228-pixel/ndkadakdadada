"""
Клиентская часть системы лицензирования.
Встраивается в jarvis.py через импорт или напрямую.
"""

import os
import sys
import json
import hashlib
import subprocess
import uuid
import winreg
import requests

# Адрес твоего сервера лицензий
LICENSE_SERVER = "http://YOUR_SERVER_IP:5000"

# Путь к файлу активации (в AppData, не рядом с EXE — труднее найти)
def _get_license_path():
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    license_dir = os.path.join(appdata, "JarvisOS")
    os.makedirs(license_dir, exist_ok=True)
    return os.path.join(license_dir, ".license")

def get_hwid():
    """
    Собирает уникальный отпечаток железа.
    Использует 5 компонентов — спуфер должен подменить ВСЕ.
    """
    components = []

    # 1. Windows Machine GUID (из реестра)
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography"
        )
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        components.append(guid)
    except Exception:
        components.append("no-guid")

    # 2. CPU ProcessorId (через WMI)
    try:
        r = subprocess.run(
            ["wmic", "cpu", "get", "ProcessorId"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if len(lines) > 1:
            components.append(lines[-1])
    except Exception:
        components.append("no-cpu")

    # 3. Материнская плата (SerialNumber)
    try:
        r = subprocess.run(
            ["wmic", "baseboard", "get", "SerialNumber"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if len(lines) > 1:
            components.append(lines[-1])
    except Exception:
        components.append("no-mb")

    # 4. Серийник диска (через WMI)
    try:
        r = subprocess.run(
            ["wmic", "diskdrive", "get", "SerialNumber"],
            capture_output=True, text=True, timeout=10
        )
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if len(lines) > 1:
            components.append(lines[-1])
    except Exception:
        components.append("no-disk")

    # 5. MAC-адрес
    components.append(str(uuid.getnode()))

    # Хэшируем всё вместе — получить исходные данные из хэша невозможно
    raw = "|".join(components)
    hwid = hashlib.sha256(raw.encode()).hexdigest()
    return hwid

def is_activated():
    """Проверяет, активирована ли программа на этом устройстве."""
    license_path = _get_license_path()
    if not os.path.exists(license_path):
        return False, None

    try:
        with open(license_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None

    hwid = get_hwid()

    # Проверяем, что HWID совпадает (защита от копирования файла активации)
    if data.get("hwid") != hwid:
        return False, None

    # Опционально: онлайн-проверка (раскомментируй для максимальной защиты)
    # try:
    #     r = requests.post(f"{LICENSE_SERVER}/verify", json={
    #         "key": data.get("key"),
    #         "hwid": hwid,
    #         "token": data.get("token")
    #     }, timeout=10)
    #     if r.status_code != 200:
    #         return False, None
    # except Exception:
    #     pass  # Если сервер недоступен — разрешаем (оффлайн-режим)

    return True, data

def activate_key(key):
    """
    Отправляет ключ на сервер для активации.
    Возвращает (True, token) при успехе или (False, error_msg).
    """
    hwid = get_hwid()

    try:
        r = requests.post(
            f"{LICENSE_SERVER}/activate",
            json={"key": key, "hwid": hwid},
            timeout=15
        )
        data = r.json()

        if data.get("ok"):
            # Сохраняем локально
            license_data = {
                "key": key.upper(),
                "hwid": hwid,
                "token": data.get("token"),
                "activated_at": str(datetime.now()) if 'datetime' in dir() else ""
            }
            license_path = _get_license_path()
            with open(license_path, "w", encoding="utf-8") as f:
                json.dump(license_data, f)

            # Скрываем файл (атрибут "скрытый" в Windows)
            try:
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(
                    license_path, 0x2  # FILE_ATTRIBUTE_HIDDEN
                )
            except Exception:
                pass

            return True, data.get("message", "Активировано")
        else:
            return False, data.get("error", "Ошибка активации")

    except requests.exceptions.ConnectionError:
        return False, "Нет связи с сервером. Проверьте интернет."
    except Exception as e:
        return False, f"Ошибка: {e}"
