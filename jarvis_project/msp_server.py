from mcp.server import MCPServer
import subprocess
import os
import sys

mcp = MCPServer("Jarvis")


# --- УПРАВЛЕНИЕ ГРОМКОСТЬЮ ЧЕРЕЗ WINDOWS CORE AUDIO API (без nircmd) ---

def _set_system_volume(level: float) -> bool:
    """Устанавливает системную громкость (0.0 — 1.0) через pycaw."""
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None
        )
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        volume.SetMasterVolumeLevelScalar(level, None)
        return True
    except ImportError:
        return False
    except Exception:
        return False


def _get_system_volume() -> float:
    """Возвращает текущую громкость (0.0 — 1.0) или -1 при ошибке."""
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(
            IAudioEndpointVolume._iid_, CLSCTX_ALL, None
        )
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        return volume.GetMasterVolumeLevelScalar()
    except Exception:
        return -1.0


@mcp.tool()
def launch_program(program_path: str) -> str:
    """Запускает программу. Можно передать имя (notepad) или полный путь."""
    try:
        if not os.path.isabs(program_path) and os.sep not in program_path:
            # Пробуем найти в PATH
            from shutil import which
            full_path = which(program_path)
            if full_path:
                program_path = full_path
            else:
                # Пробуем как системную команду
                pass
        subprocess.Popen(program_path, shell=True)
        return f"Программа запущена: {os.path.basename(program_path)}"
    except Exception as e:
        return f"Ошибка запуска: {e}"


@mcp.tool()
def run_command(command: str) -> str:
    """Выполняет команду в командной строке. Пример: dir C:\\ или tasklist"""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=30
        )
        output = result.stdout or result.stderr or "Команда выполнена (без вывода)"
        return output[:500]
    except subprocess.TimeoutExpired:
        return "Команда не успела выполниться за 30 секунд."
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def open_website(url: str) -> str:
    """Открывает сайт. Можно ввести youtube.com или https://google.com"""
    import webbrowser
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return f"Открываю {url}"
    except Exception as e:
        return f"Не удалось открыть сайт: {e}"


@mcp.tool()
def get_system_info() -> str:
    """Информация о CPU и RAM."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory()
        used_gb = mem.used // (1024**3)
        total_gb = mem.total // (1024**3)
        return f"CPU: {cpu}%, RAM: {mem.percent}% ({used_gb} ГБ из {total_gb} ГБ)"
    except ImportError:
        return "Модуль psutil не найден. Установи: pip install psutil"
    except Exception as e:
        return f"Ошибка получения информации: {e}"


@mcp.tool()
def set_volume(level: int) -> str:
    """Устанавливает громкость системы от 0 до 100. Например: set_volume(50)"""
    if level < 0 or level > 100:
        return "Уровень должен быть от 0 до 100."

    # Пытаемся через pycaw (прямой доступ к Windows Core Audio API)
    if _set_system_volume(level / 100.0):
        return f"Громкость установлена на {level}%."

    # Fallback: используем SendKeys (относительная регулировка)
    try:
        current = _get_system_volume()
        if current >= 0:
            diff = (level / 100.0) - current
            steps = int(abs(diff) * 50)
            key = 175 if diff > 0 else 174  # Volume Up / Volume Down
            for _ in range(steps):
                subprocess.run(
                    ["powershell", "-Command",
                     f"(New-Object -ComObject WScript.Shell).SendKeys([char]{key})"],
                    capture_output=True, timeout=3
                )
            return f"Громкость примерно {level}% (через SendKeys)."
        else:
            return ("Не удалось управлять громкостью. Установи pycaw: "
                    "pip install pycaw comtypes")
    except Exception as e:
        return f"Не удалось изменить громкость: {e}. Установи pycaw: pip install pycaw comtypes"


@mcp.tool()
def run_ahk_script(script_path: str) -> str:
    """Запускает .ahk скрипт. Нужен установленный AutoHotkey."""
    if not os.path.exists(script_path):
        return f"Файл не найден: {script_path}"
    try:
        subprocess.Popen(["AutoHotkey.exe", script_path])
        return f"AHK-скрипт запущен: {os.path.basename(script_path)}"
    except Exception as e:
        return f"Ошибка запуска AHK: {e}. Убедись, что AutoHotkey установлен и добавлен в PATH."


@mcp.tool()
def kill_process(process_name: str) -> str:
    """Завершает процесс. Вводи имя без .exe, например 'notepad' или 'dota2'."""
    try:
        proc_name = process_name
        if not proc_name.endswith(".exe"):
            proc_name += ".exe"

        result = subprocess.run(
            ["taskkill", "/F", "/IM", proc_name],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return f"Процесс {process_name} завершён."
        else:
            return f"Не удалось завершить {process_name}. Возможно, процесс не найден или нет прав."
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
def tell_joke() -> str:
    """Рассказывает случайную шутку."""
    import random
    jokes = [
        "Почему программисты путают Хэллоуин и Рождество? Потому что Oct 31 == Dec 25.",
        "Сколько программистов нужно, чтобы поменять лампочку? Ни одного — это аппаратная проблема.",
        "Лучший комментарий в коде: 'Я не знаю, зачем это нужно, но если убрать — всё ломается.'",
        "Босс, я не баг, я фича!",
        "Сервер говорит: 'Я устал'. А я такой: 'Мы все устали'.",
    ]
    return random.choice(jokes)


if __name__ == "__main__":
    print("[MCP] Запуск сервера Jarvis...")
    mcp.run(transport="stdio")
