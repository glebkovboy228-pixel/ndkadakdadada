"""
Патчер v11 — добавляет систему лицензирования в jarvis.py
Запуск: python patch_jarvis.py
"""

import os
import shutil

TARGET = "jarvis.py"
BACKUP = "jarvis.py.bak"

LICENSE_IMPORTS = '''
# ============================================================
#  СИСТЕМА ЛИЦЕНЗИРОВАНИЯ
# ============================================================

LICENSE_SERVER = "http://YOUR_SERVER_IP:5000"  # <<< СМЕНИ НА СВОЙ IP

def _get_license_path():
    """Путь к файлу лицензии в AppData (скрыт от пользователя)."""
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    license_dir = os.path.join(appdata, "JarvisOS")
    os.makedirs(license_dir, exist_ok=True)
    return os.path.join(license_dir, ".license")

def get_hwid():
    """Уникальный отпечаток железа (5 компонентов)."""
    components = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Microsoft\\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        components.append(guid)
    except Exception:
        components.append("no-guid")
    try:
        r = subprocess.run(["wmic", "cpu", "get", "ProcessorId"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split('\\n') if l.strip()]
        if len(lines) > 1: components.append(lines[-1])
    except Exception:
        components.append("no-cpu")
    try:
        r = subprocess.run(["wmic", "baseboard", "get", "SerialNumber"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split('\\n') if l.strip()]
        if len(lines) > 1: components.append(lines[-1])
    except Exception:
        components.append("no-mb")
    try:
        r = subprocess.run(["wmic", "diskdrive", "get", "SerialNumber"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split('\\n') if l.strip()]
        if len(lines) > 1: components.append(lines[-1])
    except Exception:
        components.append("no-disk")
    import uuid as _uuid
    components.append(str(_uuid.getnode()))
    raw = "|".join(components)
    return hashlib.sha256(raw.encode()).hexdigest()

def is_activated():
    """Проверяет, активирована ли программа."""
    license_path = _get_license_path()
    if not os.path.exists(license_path):
        return False
    try:
        with open(license_path, "r", encoding="utf-8") as f:
            import json as _json
            data = _json.load(f)
        return data.get("hwid") == get_hwid()
    except Exception:
        return False

def activate_key(key):
    """Активирует ключ на сервере. Возвращает dict с результатом."""
    hwid = get_hwid()
    try:
        r = requests.post(
            f"{LICENSE_SERVER}/activate",
            json={"key": key, "hwid": hwid},
            timeout=15
        )
        data = r.json()
        if data.get("ok"):
            import json as _json
            license_path = _get_license_path()
            with open(license_path, "w", encoding="utf-8") as f:
                _json.dump({
                    "key": key.upper(),
                    "hwid": hwid,
                    "token": data.get("token", "")
                }, f)
            try:
                ctypes.windll.kernel32.SetFileAttributesW(license_path, 0x2)
            except Exception:
                pass
        return data
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Нет связи с сервером активации. Проверьте интернет."}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка: {e}"}


# ============================================================
#  API для экрана активации
# ============================================================

class ActivationApi:
    """API для экрана активации (до загрузки основного интерфейса)."""
    def __init__(self):
        pass

    def activate(self, key):
        result = activate_key(key)
        if result.get("ok"):
            return {"ok": True, "message": "Активация успешна! Загрузка..."}
        else:
            return {"ok": False, "error": result.get("error", "Ошибка активации")}

    def load_main(self):
        """Закрывает окно активации и открывает основное."""
        import webview
        # Уничтожаем текущее окно
        webview.windows[0].destroy()
        # Открываем основное
        _start_main_app()

'''

NEW_MAIN = '''def main():
    global PROGRAMS

    # --- ПРОВЕРКА ЛИЦЕНЗИИ ---
    if is_activated():
        _start_main_app()
        return

    # --- ЭКРАН АКТИВАЦИИ ---
    activation_html = os.path.join(_BASE_DIR, "jarvis_os", "activation.html")
    api = ActivationApi()

    webview.create_window(
        'JARVIS OS — Активация',
        activation_html,
        width=600,
        height=500,
        resizable=False,
        frameless=True,
        easy_drag=True,
        js_api=api,
    )
    webview.start()


def _start_main_app():
    """Запуск основного приложения после проверки лицензии."""
    global PROGRAMS
    PROGRAMS = _build_programs_dict()

    for game_name in _STEAM_GAMES:
        if game_name in PROGRAMS:
            QUICK_GAMES[game_name] = (PROGRAMS[game_name], game_name.title())

    brain = FridayBrain()
    api = PyWebViewApi(brain)

    brain.log(f"Программ: {len(PROGRAMS)}")
    brain.log(f"Микрофон: {MIC_SAMPLE_RATE} Гц -> {ASR_SAMPLE_RATE} Гц")
    brain.log(f"ASR: {ASR_MODEL_NAME}")
    brain.log(f"LLM: {LLM_MODEL}")
    brain.log("TTS: Edge TTS (DariyaNeural) with emotions")
    if HAS_BARK:
        brain.log("Bark TTS: доступен (скажи: Пятница, включи барк)")
    else:
        brain.log("Bark TTS: не установлен")
    brain.log("Веб-поиск: Wikipedia + DuckDuckGo + стихи")
    brain.log("Стоп: прерывает думание и говорение")
    brain.log("ИИ-ассистент: отвечает на любые вопросы")

    html_path = os.path.join(_BASE_DIR, "jarvis_os", "index.html")

    webview.create_window(
        'JARVIS // FRIDAY OS',
        html_path,
        width=1280,
        height=800,
        min_size=(900, 600),
        frameless=True,
        easy_drag=True,
        js_api=api,
    )

    def _brain_thread():
        brain.start_model()
        try:
            while brain.is_listening:
                time.sleep(1)
        except Exception:
            pass

    threading.Thread(target=_brain_thread, daemon=True).start()
    webview.start()
    brain.stop()


if __name__ == "__main__":
    main()
'''


def main():
    if not os.path.exists(TARGET):
        print(f"[!] Файл {TARGET} не найден!")
        return

    with open(TARGET, "r", encoding="utf-8") as f:
        content = f.read()

    shutil.copy2(TARGET, BACKUP)
    print(f"[OK] Бэкап: {BACKUP}")

    patched = content
    changes = 0

    # 1. Добавить систему лицензирования перед def main()
    if "get_hwid" not in patched or "is_activated" not in patched:
        lines = patched.split('\n')
        main_idx = None
        for i, line in enumerate(lines):
            if line.strip() == "def main():" or (line.strip().startswith("def main(") and not line.startswith(' ') and not line.startswith('\\t')):
                main_idx = i
                break

        if main_idx is not None:
            lines.insert(main_idx, LICENSE_IMPORTS)
            patched = '\n'.join(lines)
            changes += 1
            print("[OK] Добавлена система лицензирования")
        else:
            print("[ERROR] Не найдена def main()")
    else:
        print("[SKIP] Система лицензирования уже есть")

    # 2. Переписать main() и добавить _start_main_app()
    if "_start_main_app" not in patched:
        lines = patched.split('\n')
        main_start = None
        main_end = None

        for i, line in enumerate(lines):
            if line.strip() == "def main():" or (line.strip().startswith("def main(") and not line.startswith(' ') and not line.startswith('\\t')):
                main_start = i
                break

        if main_start is not None:
            for i in range(main_start + 1, len(lines)):
                if lines[i].strip().startswith("if __name__"):
                    main_end = i
                    break

            if main_end is not None:
                new_lines = lines[:main_start] + NEW_MAIN.split('\n') + lines[main_end + 1:]
                patched = '\n'.join(new_lines)
                changes += 1
                print("[OK] main() переписана с проверкой лицензии")
            else:
                print("[ERROR] Не найден if __name__")
        else:
            print("[ERROR] Не найдена def main()")
    else:
        print("[SKIP] _start_main_app уже есть")

    if changes > 0:
        with open(TARGET, "w", encoding="utf-8") as f:
            f.write(patched)
        print(f"\n[OK] Патч применён. Изменений: {changes}")
        print("\n[!] НЕ ЗАБУДЬ: Смени LICENSE_SERVER на свой IP в jarvis.py!")
    else:
        print("\n[OK] Изменений не требуется")


if __name__ == "__main__":
    main()
