import os
import sys
import io

# --- ФИКС: в EXE (--windowed) нет консоли, print() с кириллицей падает ---
if getattr(sys, 'frozen', False):
    try:
        sys.stdout = io.TextIOWrapper(open(os.devnull, 'w'), encoding='utf-8')
        sys.stderr = io.TextIOWrapper(open(os.devnull, 'w'), encoding='utf-8')
    except Exception:
        sys.stdout = None
        sys.stderr = None
# ----------------------------------------------------------------------

import os
import sys
import json
import re
import threading
import subprocess
import asyncio
import tempfile
import urllib.parse
import time
import traceback
from datetime import datetime, timedelta
from difflib import get_close_matches
import random
import hashlib

import requests
import webview
import xml.etree.ElementTree as ET

try:
    import ollama
    HAS_OLLAMA = True
except Exception:
    HAS_OLLAMA = False
    print("[OLLAMA] не установлена — fallback отключён")
from openai import OpenAI

import edge_tts
import sounddevice as sd
import soundfile as sf
import numpy as np
from scipy.signal import butter, sosfilt, resample_poly

try:
    import torch
    HAS_TORCH = True
except Exception:
    HAS_TORCH = False
    print("[TTS] PyTorch не установлен! pip install torch")
    print("[TTS] Будет использован edge_tts как fallback")

try:
    os.environ["SUNO_USE_SMALL_MODELS"] = "True"
    os.environ["SUNO_OFFLOAD_CPU"] = "True"
    from bark import SAMPLE_RATE as BARK_SAMPLE_RATE, generate_audio as bark_generate, preload_models as bark_preload
    HAS_BARK = True
except Exception:
    HAS_BARK = False
    print("[TTS] Bark не установлен — pip install git+https://github.com/suno-ai/bark.git")

try:
    import onnx_asr
    HAS_ONNX_ASR = True
except Exception:
    HAS_ONNX_ASR = False

try:
    import keyboard
    HAS_KEYBOARD = True
except Exception:
    HAS_KEYBOARD = False

try:
    from PIL import ImageGrab
    HAS_PIL = True
except Exception:
    HAS_PIL = False

try:
    import ctypes
    HAS_CTYPES = True
except Exception:
    HAS_CTYPES = False

try:
    from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    from ctypes import cast, POINTER
    import comtypes
    HAS_PYCAW = True
except Exception:
    HAS_PYCAW = False
    print("[VOL] pycaw не установлен! pip install pycaw comtypes")


# ============================================================
#  ПУТИ И КОНФИГИ
# ============================================================

if getattr(sys, 'frozen', False):
    _BASE_DIR = sys._MEIPASS
    _APP_DIR = os.path.dirname(sys.executable)
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    _APP_DIR = _BASE_DIR

LLM_MODEL = "qwen2.5:1.5b"          # локальная модель (быстро на CPU)
LLM_CLOUD_MODEL = "inclusionai/ling-3.0-flash-vl:free"

# Клиент ZvenoAI (бесплатно, без карты, работает в РФ)
_cloud_client = OpenAI(
    api_key="sk-pUaH7TdRzbbXUrSEJI1g69NpCP90zI9n-5GrH7W9Mn0",
    base_url="https://api.zveno.ai/v1",
)








ASR_MODEL_NAME = "gigaam-v3-e2e-rnnt"

SILERO_SPEAKER = "baya"
SILERO_SAMPLE_RATE = 48000

CORRECTIONS_PATH = os.path.join(_APP_DIR, "corrections.json")
LEARNING_LOG_PATH = os.path.join(_APP_DIR, "learning_log.json")

MIC_SAMPLE_RATE = 48000
ASR_SAMPLE_RATE = 16000

SILENCE_THRESHOLD = 120
SILENCE_WAIT = 1.8  # быстро фиксирует конец фразы — меньше latency
MAX_PHRASE_DURATION = 20.0  # длинные фразы не обрежутся

DIALOG_HISTORY = []
DIALOG_HISTORY_MAX = 5

_active_timers = {}
_timer_lock = threading.Lock()

_stop_event = threading.Event()


# ============================================================
#  БЕЗОПАСНАЯ РАБОТА С JSON
# ============================================================

def _safe_load_json(path: str, default: dict) -> dict:
    try:
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=4)
            return default
        with open(path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        if not content:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=4)
            return default
        return json.loads(content)
    except (json.JSONDecodeError, Exception) as e:
        print(f"[CORRECTIONS] Файл битый, пересоздаю: {e}")
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=4)
        except Exception:
            pass
        return default


def _safe_save_json(path: str, data: dict):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"[JSON] Ошибка сохранения: {e}")


# ============================================================
#  SELF LEARNER
# ============================================================

class SelfLearner:
    def __init__(self):
        self.learned_programs = {}
        self._lock = threading.Lock()

    def _log_learning(self, event_type: str, details: str):
        try:
            log = []
            if os.path.exists(LEARNING_LOG_PATH):
                with open(LEARNING_LOG_PATH, "r", encoding="utf-8") as f:
                    log = json.load(f)
            log.append({
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "type": event_type,
                "details": details,
            })
            log = log[-500:]
            with open(LEARNING_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(log, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[LEARN] Ошибка записи лога: {e}")

    def learn_program(self, name: str, path: str) -> bool:
        with self._lock:
            name = name.lower().strip()
            path = path.strip()
            if not name or not path:
                return False
            self.learned_programs[name] = path
            self._log_learning("new_program", f"'{name}' -> '{path}'")
            global PROGRAMS
            PROGRAMS[name] = f'start "" "{path}"'
            data = _safe_load_json(CORRECTIONS_PATH, {"friday_variants": [], "custom_programs": {}})
            if "custom_programs" not in data:
                data["custom_programs"] = {}
            data["custom_programs"][name] = path
            _safe_save_json(CORRECTIONS_PATH, data)
            return True

    def load_custom_programs(self):
        global PROGRAMS
        data = _safe_load_json(CORRECTIONS_PATH, {"friday_variants": [], "custom_programs": {}})
        custom = data.get("custom_programs", {})
        for name, path in custom.items():
            PROGRAMS[name.lower()] = f'start "" "{path}"'
            self.learned_programs[name.lower()] = path
        if custom:
            print(f"[LEARN] Загружено кастомных программ: {len(custom)}")


# ============================================================
#  ПРОГРАММЫ
# ============================================================

PROGRAMS = {}  # Заполняется автоматически при запуске


# ============================================================
#  АВТООПРЕДЕЛЕНИЕ ПУТЕЙ К ПРОГРАММАМ
# ============================================================

import winreg

def _search_exe_in_dirs(exe_name: str, dirs: list) -> str | None:
    """Ищет exe-файл в списке директорий."""
    for d in dirs:
        if not d or not os.path.isdir(d):
            continue
        try:
            for item in os.listdir(d):
                if item.lower() == exe_name.lower():
                    return os.path.join(d, item)
                full = os.path.join(d, item)
                if os.path.isdir(full):
                    try:
                        for sub in os.listdir(full):
                            if sub.lower() == exe_name.lower():
                                return os.path.join(full, sub)
                    except (PermissionError, OSError):
                        pass
        except (PermissionError, OSError):
            pass
    return None


def _search_exe_deep(exe_name: str, base_dirs: list, depth: int = 2) -> str | None:
    """Ищет exe-файл рекурсивно до указанной глубины."""
    for base in base_dirs:
        if not base or not os.path.isdir(base):
            continue
        try:
            for root, dirs, files in os.walk(base):
                rel = os.path.relpath(root, base)
                if rel.count(os.sep) >= depth:
                    dirs.clear()
                    continue
                for f in files:
                    if f.lower() == exe_name.lower():
                        return os.path.join(root, f)
        except (PermissionError, OSError):
            pass
    return None


def _get_program_files_dirs() -> list:
    """Возвращает все возможные Program Files директории."""
    dirs = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    dirs.append(pf)
    dirs.append(pf86)
    return dirs


def _get_appdata_dirs() -> list:
    """Возвращает директории AppData пользователя."""
    dirs = []
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    home = os.path.expanduser("~")
    if local:
        dirs.append(local)
    if roaming:
        dirs.append(roaming)
    dirs.append(home)
    return dirs


def _find_steam_path() -> str | None:
    """Ищет путь к Steam через реестр."""
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam")
        steam_path, _ = winreg.QueryValueEx(key, "SteamPath")
        winreg.CloseKey(key)
        if steam_path and os.path.isdir(steam_path):
            return steam_path
    except Exception:
        pass
    # Fallback: типичные пути
    for p in [r"C:\Program Files (x86)\Steam",
              r"C:\Program Files\Steam",
              os.path.join(os.environ.get("LOCALAPPDATA", ""), "Steam")]:
        if os.path.isdir(p):
            return p
    return None


def _find_steam_game(exe_name: str, game_dir: str) -> str | None:
    """Ищет игру в библиотеке Steam."""
    steam = _find_steam_path()
    if not steam:
        return None
    # Читаем libraryfolders.vdf для доп. библиотек
    libraries = [steam]
    vdf_path = os.path.join(steam, "steamapps", "libraryfolders.vdf")
    if os.path.exists(vdf_path):
        try:
            with open(vdf_path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if '"path"' in line:
                        parts = line.split('"')
                        if len(parts) >= 4:
                            lib = parts[3].replace("/", os.sep).replace("\\", os.sep)
                            if os.path.isdir(lib):
                                libraries.append(lib)
        except Exception:
            pass
    for lib in libraries:
        game_path = os.path.join(lib, "steamapps", "common", game_dir)
        if os.path.isdir(game_path):
            result = _search_exe_deep(exe_name, [game_path], depth=5)
            if result:
                return result
    return None


SEP = chr(92)

def _find_via_where(exe_name):
    try:
        r = subprocess.run(["where", exe_name], capture_output=True, text=True, timeout=5, shell=True)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().split(chr(10))[0].strip()
    except Exception: pass
    return None

def _find_via_registry(exe_name):
    base = SEP.join(["SOFTWARE", "Microsoft", "Windows", "CurrentVersion", "App Paths"]) + SEP
    for hkey in [winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER]:
        try:
            key = winreg.OpenKey(hkey, base + exe_name)
            val, _ = winreg.QueryValueEx(key, None)
            winreg.CloseKey(key)
            if val and os.path.exists(val): return val
        except Exception: pass
    base2 = SEP.join(["SOFTWARE", "WOW6432Node", "Microsoft", "Windows", "CurrentVersion", "App Paths"]) + SEP
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base2 + exe_name)
        val, _ = winreg.QueryValueEx(key, None)
        winreg.CloseKey(key)
        if val and os.path.exists(val): return val
    except Exception: pass
    return None

def _find_via_start_menu(exe_name):
    sm_dirs = []
    local = os.environ.get("APPDATA", "")
    common = os.environ.get("ProgramData", os.path.join("C:", "ProgramData"))
    if local: sm_dirs.append(os.path.join(local, "Microsoft", "Windows", "Start Menu", "Programs"))
    if common: sm_dirs.append(os.path.join(common, "Microsoft", "Windows", "Start Menu", "Programs"))
    exe_lower = exe_name.lower().replace(".exe", "")
    for d in sm_dirs:
        if not os.path.isdir(d): continue
        try:
            for root, dirs, files in os.walk(d):
                for f in files:
                    if f.lower().endswith(".lnk") and exe_lower in f.lower():
                        try:
                            lnk = os.path.join(root, f)
                            cmd = "(New-Object -ComObject WScript.Shell).CreateShortcut('" + lnk + "').TargetPath"
                            ps = subprocess.run(["powershell", "-Command", cmd], capture_output=True, text=True, timeout=5)
                            if ps.returncode == 0 and ps.stdout.strip() and os.path.exists(ps.stdout.strip()):
                                return ps.stdout.strip()
                        except Exception: pass
        except Exception: pass
    return None

# Реестр программ: имя -> (exe, [директории для поиска])
_PROGRAM_REGISTRY = {
    "chrome": ("chrome.exe", [
        r"{PF}\Google\Chrome\Application",
        r"{PF86}\Google\Chrome\Application",
        r"{LOCALAPPDATA}\Google\Chrome\Application",
    ]),
    "firefox": ("firefox.exe", [
        r"{PF}\Mozilla Firefox",
        r"{PF86}\Mozilla Firefox",
    ]),
    "opera": ("opera.exe", [
        r"{LOCALAPPDATA}\Programs\Opera",
        r"{PF}\Opera",
    ]),
    "discord": ("Discord.exe", [
        r"{LOCALAPPDATA}\Discord",
        r"{LOCALAPPDATA}\Discord\app-*",  # wildcard
    ]),
    "telegram": ("Telegram.exe", [
        r"{LOCALAPPDATA}\Programs\Telegram Desktop",
        r"{APPDATA}\Telegram Desktop",
    ]),
    "spotify": ("Spotify.exe", [
        r"{LOCALAPPDATA}\Spotify",
        r"{PF}\Spotify",
        r"{PF86}\Spotify",
    ]),
    "vscode": ("Code.exe", [
        r"{LOCALAPPDATA}\Programs\Microsoft VS Code",
        r"{PF}\Microsoft VS Code",
        r"{PF86}\Microsoft VS Code",
    ]),
    "vlc": ("vlc.exe", [
        r"{PF}\VideoLAN\VLC",
        r"{PF86}\VideoLAN\VLC",
    ]),
    "steam": ("steam.exe", [
        r"{PF86}\Steam",
        r"{PF}\Steam",
    ]),
    "minecraft": ("MinecraftLauncher.exe", [
        r"{PF}\Minecraft Launcher",
        r"{PF86}\Minecraft Launcher",
        r"{LOCALAPPDATA}\Programs\Minecraft Launcher",
    ]),
    "obs": ("obs64.exe", [
        r"{PF}\obs-studio\bin\64bit",
        r"{PF86}\obs-studio\bin\64bit",
    ]),
    "blender": ("blender.exe", [
        r"{PF}\Blender Foundation\Blender",
        r"{PF86}\Blender Foundation\Blender",
    ]),
    "notepad++": ("notepad++.exe", [
        r"{PF}\Notepad++",
        r"{PF86}\Notepad++",
    ]),
    "7zip": ("7zFM.exe", [
        r"{PF}\7-Zip",
        r"{PF86}\7-Zip",
    ]),
    "winrar": ("WinRAR.exe", [
        r"{PF}\WinRAR",
        r"{PF86}\WinRAR",
    ]),
    "photoshop": ("Photoshop.exe", [
        r"{PF}\Adobe\Adobe Photoshop *",
        r"{PF86}\Adobe\Adobe Photoshop *",
    ]),
    "gimp": (r"gimp-*\bin\gimp-*.exe", [
        r"{PF}\GIMP 2",
        r"{PF86}\GIMP 2",
    ]),
    "zoom": ("Zoom.exe", [
        r"{LOCALAPPDATA}\Zoom",
        r"{PF}\Zoom",
        r"{PF86}\Zoom",
    ]),
    "notion": ("Notion.exe", [
        r"{LOCALAPPDATA}\Programs\Notion",
        r"{LOCALAPPDATA}\Notion",
    ]),
    "twitch": ("Twitch.exe", [
        r"{PF}\Twitch",
        r"{PF86}\Twitch",
        r"{LOCALAPPDATA}\Twitch",
    ]),
    "edge": ("msedge.exe", [r"{PF}\Microsoft\Edge\Application", r"{PF86}\Microsoft\Edge\Application"]),
    "yandex": ("browser.exe", [r"{PF}\Yandex\YandexBrowser\Application", r"{PF86}\Yandex\YandexBrowser\Application"]),
    "thunderbird": ("thunderbird.exe", [r"{PF}\Mozilla Thunderbird", r"{PF86}\Mozilla Thunderbird"]),
    "audacity": ("audacity.exe", [r"{PF}\Audacity", r"{PF86}\Audacity"]),
    "obsidian": ("Obsidian.exe", [r"{LOCALAPPDATA}\Obsidian", r"{LOCALAPPDATA}\Programs\Obsidian"]),
    "davinci": ("Resolve.exe", [r"{PF}\Blackmagic Design\DaVinci Resolve"]),
    "intellij": ("idea64.exe", [r"{PF}\JetBrains\IntelliJ IDEA *\bin"]),
    "webstorm": ("webstorm64.exe", [r"{PF}\JetBrains\WebStorm *\bin"]),
    "rider": ("rider64.exe", [r"{PF}\JetBrains\JetBrains Rider *\bin"]),
    "github": ("GitHubDesktop.exe", [r"{LOCALAPPDATA}\GitHubDesktop", r"{LOCALAPPDATA}\Programs\GitHub Desktop"]),
    "epic": ("EpicGamesLauncher.exe", [r"{PF}\Epic Games", r"{PF86}\Epic Games"]),
    "battle.net": ("Battle.net.exe", [r"{PF}\Battle.net", r"{PF86}\Battle.net"]),
    "roblox": ("RobloxPlayerBeta.exe", [r"{LOCALAPPDATA}\Roblox\Versions"]),
    "utorrent": ("uTorrent.exe", [r"{APPDATA}\uTorrent"]),
    "qbittorrent": ("qbittorrent.exe", [r"{PF}\qBittorrent", r"{PF86}\qBittorrent"]),
    "anydesk": ("AnyDesk.exe", [r"{PF}\AnyDesk", r"{PF86}\AnyDesk"]),
    "teamviewer": ("TeamViewer.exe", [r"{PF}\TeamViewer", r"{PF86}\TeamViewer"]),
}

# Игры Steam: имя -> (exe, папка игры)
_STEAM_GAMES = {
    "dota 2": ("dota2.exe", "dota 2 beta"),
    "dota": ("dota2.exe", "dota 2 beta"),
    "cs2": ("cs2.exe", "Counter-Strike Global Offensive"),
    "cs go": ("csgo.exe", "Counter-Strike Global Offensive"),
    "gta v": ("GTA5.exe", "Grand Theft Auto V"),
    "gta 5": ("GTA5.exe", "Grand Theft Auto V"),
    "cyberpunk": ("Cyberpunk2077.exe", "Cyberpunk 2077"),
    "elden ring": ("ELDEN RING.exe", "ELDEN RING"),
    "skyrim": ("SkyrimSE.exe", "Skyrim Special Edition"),
    "terraria": ("Terraria.exe", "Terraria"),
    "rust": ("RustClient.exe", "rust"),
    "ark": ("ShooterGame.exe", "ARK"),
    "warframe": ("Warframe.exe", "Warframe"),
    "dont starve": ("dontstarve_steam.exe", "Don't Starve Together"),
    "the forest": ("TheForest.exe", "TheForest"),
    "raft": ("Raft.exe", "Raft"),
    "subnautica": ("Subnautica.exe", "Subnautica"),
    "factorio": ("factorio.exe", "Factorio"),
    "satisfactory": ("FactoryGameSteam-Win64-Shipping.exe", "Satisfactory"),
    "dead cells": ("deadcells.exe", "Dead Cells"),
    "hollow knight": ("hollow_knight.exe", "Hollow Knight"),
    "hades": ("Hades.exe", "Hades"),
    "stardew valley": ("Stardew Valley.exe", "Stardew Valley"),
    "portal 2": ("portal2.exe", "Portal 2"),
    "baldurs gate 3": ("bg3.exe", "Baldurs Gate 3"),
    "helldivers 2": ("helldivers2.exe", "Helldivers 2"),
    "palworld": ("PalWorld.exe", "Palworld"),
    "fc 25": ("FC25.exe", "EA SPORTS FC 25"),
    "fc 26": ("FC26.exe", "EA SPORTS FC 26"),
    "фифа 25": ("FC25.exe", "EA SPORTS FC 25"),
    "фифа 26": ("FC26.exe", "EA SPORTS FC 26"),
}


def _expand_path_template(template: str) -> str:
    """Заменяет плейсхолдеры в пути на реальные значения."""
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local = os.environ.get("LOCALAPPDATA", "")
    roaming = os.environ.get("APPDATA", "")
    return (template
            .replace("{PF}", pf)
            .replace("{PF86}", pf86)
            .replace("{LOCALAPPDATA}", local)
            .replace("{APPDATA}", roaming))


def _detect_programs():
    """Автоматически находит пути к программам на текущем ПК."""
    detected = {}
    print("[DETECT] Сканирую установленные программы...")

    for key, (exe_name, dir_templates) in _PROGRAM_REGISTRY.items():
        dirs = [_expand_path_template(t) for t in dir_templates]
        # Пробуем точные пути
        path = _search_exe_in_dirs(exe_name, dirs)
        if not path:
            # Пробуем глубокий поиск
            path = _search_exe_deep(exe_name, dirs, depth=2)
        if not path:
            # Пробуем в Program Files в целом
            pf_dirs = _get_program_files_dirs()
            path = _search_exe_deep(exe_name, pf_dirs, depth=4)
        # 4. where
        if not path: path = _find_via_where(exe_name)
        # 5. registry
        if not path: path = _find_via_registry(exe_name)
        # 6. start menu
        if not path: path = _find_via_start_menu(exe_name)
        if path:
            detected[key] = path
            print(f"  [OK] {key}: {path}")
        else:
            print(f"  [--] {key}: не найдено")

    # Игры Steam
    steam_games_found = 0
    for game_name, (exe_name, game_dir) in _STEAM_GAMES.items():
        path = _find_steam_game(exe_name, game_dir)
        if path:
            detected[game_name] = path
            print(f"  [OK] {game_name}: {path}")
            steam_games_found += 1
        else:
            print(f"  [--] {game_name}: не найдено в Steam")

    print(f"[DETECT] Найдено программ: {len(detected)}, игр Steam: {steam_games_found}")
    return detected


def _build_programs_dict() -> dict:
    """Собирает финальный словарь PROGRAMS с автоопределёнными путями."""
    result = {}
    detected = _detect_programs()

    # Маппинг ключей PROGRAMS на ключи реестра
    _MAP = {
        "браузер": "chrome", "хром": "chrome", "google chrome": "chrome",
        "google": "chrome", "firefox": "firefox", "опера": "opera",
        "opera": "opera", "discord": "discord",
        "telegram": "telegram", "телеграм": "telegram", "тг": "telegram",
        "spotify": "spotify", "спотифай": "spotify",
        "vscode": "vscode", "vs code": "vscode",
        "vlc": "vlc", "steam": "steam", "стим": "steam",
        "minecraft": "minecraft", "майнкрафт": "minecraft",
        "obs": "obs", "blender": "blender", "блендер": "blender",
        "notepad++": "notepad++", "7zip": "7zip", "7 zip": "7zip",
        "winrar": "winrar", "photoshop": "photoshop", "фотошоп": "photoshop",
        "zoom": "zoom", "notion": "notion", "twitch": "twitch", "твитч": "twitch",
        "edge": "edge", "эдж": "edge", "яндекс": "yandex", "thunderbird": "thunderbird",
        "audacity": "audacity", "obsidian": "obsidian", "davinci": "davinci",
        "intellij": "intellij", "idea": "intellij", "webstorm": "webstorm",
        "rider": "rider", "github": "github", "epic": "epic",
        "battle.net": "battle.net", "батлнет": "battle.net",
        "роблокс": "roblox", "utorrent": "utorrent", "qbittorrent": "qbittorrent",
        "anydesk": "anydesk", "teamviewer": "teamviewer",
    }

    for alias, reg_key in _MAP.items():
        if reg_key in detected:
            path = detected[reg_key]
            result[alias] = f'start "" "{path}"'

    # Программы, которые запускаются по системной команде (без пути)
    _SYSTEM_PROGRAMS = {
        "word": "start winword",
        "ворд": "start winword",
        "excel": "start excel",
        "эксель": "start excel",
        "powerpoint": "start powerpnt",
        "поверпоинт": "start powerpnt",
        "pycharm": "start pycharm64",
        "код": "start code",
        "винрар": "start WinRAR",
        "роблокс": "start robloxplayer",
        "epic games": "start EpicGamesLauncher",
        "epic": "start EpicGamesLauncher",
        "battle.net": "start battle.net",
        "батлнет": "start battle.net",
        "jetbrains": "start jetbrains",
        "android studio": "start studio64",
        "git": "start git",
        "github": "start github",
        "github desktop": "start GitHubDesktop",
        "winamp": "start winamp",
        "audacity": "start audacity",
        "audition": "start audition",
        "davinci resolve": "start resolve",
        "obsidian": "start obsidian",
        "terminal": "start wt",
        "windows terminal": "start wt",
        "блокнот": "start notepad", "notepad": "start notepad",
        "калькулятор": "start calc", "calculator": "start calc",
        "paint": "start mspaint", "пейнт": "start mspaint",
        "проводник": "start explorer", "explorer": "start explorer",
        "файлы": "start explorer", "папки": "start explorer",
        "командная строка": "start cmd", "cmd": "start cmd", "консоль": "start cmd",
        "диспетчер задач": "start taskmgr",
        "панель управления": "start control",
        "настройки": "start ms-settings:", "параметры": "start ms-settings:",
        "ножницы": "start snippingtool", "скриншот": "start snippingtool",
        "powershell": "start powershell",
        "редактор реестра": "start regedit", "regedit": "start regedit",
        "службы": "start services.msc",
        "диспетчер устройств": "start devmgmt.msc",
        "управление дисками": "start diskmgmt.msc",
        "лупа": "start magnify", "экранная клавиатура": "start osk",
        "заметки": "start stikynot",
        "проигрыватель": "start wmplayer",
        "очистка диска": "start cleanmgr",
        "сведения о системе": "start msinfo32",
    }
    result.update(_SYSTEM_PROGRAMS)

    # Игры
    for game_name in _STEAM_GAMES:
        if game_name in detected:
            path = detected[game_name]
            result[game_name] = f'start "" "{path}"'

    return result

CLOSE_TARGETS = {
    "Steam.exe": ["стим", "steam"],
    "cs2.exe": ["кс 2", "cs2", "каунтер страйк", "counter strike", "кс го", "кс"],
    "dota2.exe": ["дота 2", "дота", "dota 2", "dota"],
    "FC26.exe": ["фифа 26", "fc 26", "фифа", "ea sports fc"],
    "chrome.exe": ["браузер", "хром", "chrome", "гугл", "google"],
    "firefox.exe": ["firefox", "мозилла", "фаерфокс"],
    "msedge.exe": ["edge", "эдж"],
    "opera.exe": ["опера", "opera", "опер"],
    "notepad.exe": ["блокнот", "нотепад"],
    "calc.exe": ["калькулятор"],
    "Telegram.exe": ["телеграм", "telegram", "телеграмм"],
    "Discord.exe": ["дискорд", "discord", "дискор"],
    "MinecraftLauncher.exe": ["майнкрафт", "minecraft", "маинкрафт"],
    "spotify.exe": ["спотифай", "spotify"],
    "vlc.exe": ["vlc", "влц"],
    "wmplayer.exe": ["проигрыватель"],
    "EpicGamesLauncher.exe": ["epic", "эпик"],
    "battle.net.exe": ["батлнет", "battle.net"],
    "zoom.exe": ["zoom", "зум"],
    "obs64.exe": ["obs", "обс"],
    "Photoshop.exe": ["фотошоп", "photoshop"],
    "blender.exe": ["блендер", "blender"],
    "Code.exe": ["vscode", "vs code", "код", "code"],
    "pycharm64.exe": ["pycharm", "пайчарм"],
    "obsidian.exe": ["obsidian", "обсидиан"],
    "studio64.exe": ["android studio", "андроид студия"],
}

ALIASES = {
    "открой браузер": "браузер", "запусти браузер": "браузер",
    "открой калькулятор": "калькулятор", "запусти калькулятор": "калькулятор",
    "открой блокнот": "блокнот", "запусти блокнот": "блокнот",
    "открой стим": "стим", "запусти стим": "стим", "открой steam": "steam",
    "открой телеграм": "телеграм", "запусти телеграм": "телеграм",
    "открой дискорд": "дискорд", "запусти дискорд": "дискорд",
    "открой кс 2": "cs2", "запусти кс 2": "cs2",
    "открой доту 2": "дота 2", "запусти доту 2": "дота 2",
    "открой доту": "дота 2",
    "открой фифу 26": "фифа 26", "запусти фифу 26": "фифа 26",
    "открой фифу": "фифа 26", "запусти fc 26": "fc 26",
    "открой ютуб": "ютуб", "открой youtube": "ютуб",
    "открой вк": "вк", "открой вконтакте": "вк",
    "открой проводник": "проводник", "запусти проводник": "проводник",
    "открой панель управления": "панель управления",
    "открой диспетчер задач": "диспетчер задач",
    "открой командную строку": "командная строка",
    "open browser": "браузер", "launch browser": "браузер",
    "open steam": "steam", "launch steam": "steam",
    "open discord": "discord", "launch discord": "discord",
    "open telegram": "telegram", "launch telegram": "telegram",
    "open cs2": "cs2", "launch cs2": "cs2",
    "open dota": "dota", "launch dota": "dota",
    "open dota 2": "dota 2",
    "launch dota 2": "dota 2",
    "open youtube": "youtube", "launch youtube": "youtube",
    "open vk": "вк", "launch vk": "вк",
    "open calculator": "калькулятор", "open notepad": "блокнот",
    "open code": "vscode", "launch code": "vscode",
}

TIME_TRIGGERS = [
    "время", "сколько времени", "который час", "скажи время",
    "сколько сейчас времени", "сейчас время", "который сейчас час",
    "текущее время", "узнай время", "посмотри время",
    "сказать время", "сказать сколько времени", "сколько часов",
]

LEARN_TRIGGERS = [
    "запомни программу", "запомни приложение", "добавь программу",
    "добавь приложение", "новая программа", "запомни путь",
    "добавь игру", "запомни игру",
]

WEATHER_TRIGGERS = [
    "погода", "какая погода", "прогноз погоды", "температура",
    "за окном", "weather", "forecast",
]

SYSTEM_TRIGGERS = {
    "громче": ["громче", "прибавь громкость", "увеличь громкость", "звук громче", "volume up"],
    "тише": ["тише", "убавь громкость", "уменьшь громкость", "звук тише", "volume down"],
    "без звука": ["без звука", "выключи звук", "mute", "отключи звук", "заглуши"],
    "звук включить": ["включи звук", "звук вкл", "unmute", "верни звук"],
    "блокировка": ["заблокируй", "блокировка", "lock", "залочь", "залочить"],
    "сон": ["спящий режим", "усни", "сон", "sleep", "засыпай"],
    "выключение": ["выключи компьютер", "выключи пк", "выключение", "shutdown", "выруби комп", "выключи комп"],
    "перезагрузка": ["перезагрузи", "перезагрузка", "restart", "reboot", "ребут"],
}

JOKE_TRIGGERS = ["шутк", "пошути", "анекдот", "рассмеши", "joke"]

TIMER_TRIGGERS = ["таймер", "напомни", "напоминание", "поставь таймер", "засеки", "timer", "remind"]

SCREENSHOT_TRIGGERS = ["скриншот", "снимок экрана", "скрин", "screenshot", "фото экрана", "сфотографируй экран"]

NEWS_TRIGGERS = ["новости", "что в мире", "события", "news", "что нового", "заголовки"]

BATTERY_TRIGGERS = ["батарея", "заряд", "battery", "сколько заряда", "уровень заряда"]

STOP_TRIGGERS = ["стоп", "хватит", "остановись", "прерви", "отмена", "stop", "останови", "перебей", "молчи"]

MODEL_TRIGGERS = [
    "покажи костюм", "покажи броню", "модель костюма", "модель брони",
    "железный человек", "iron man", "покажи модель", "3d модель",
    "костюм железного человека", "броня железного человека",
    "mark 85", "марк 85", "покажи железного человека",
    "покажи доспех", "модель железного человека",
    "костюм", "броню", "броня", "доспех",
]

MODEL_CLOSE_TRIGGERS = [
    "убери костюм", "закрой костюм", "скрой костюм", "спрячь костюм",
    "убери броню", "закрой броню", "скрой броню", "спрячь броню",
    "убери модель", "закрой модель", "скрой модель", "спрячь модель",
    "убери 3d", "закрой 3d", "убери железного человека",
    "закрой железного человека", "спрячь железного человека",
]

JOKES = [
    "Почему программисты путают Хэллоуин и Рождество? Потому что Oct 31 = Dec 25.",
    "Заходит как-то SQL-запрос в бар, подходит к двум столикам и спрашивает: «Можно к вам?».",
    "Программист идёт в магазин. Жена говорит: «Купи батон хлеба, если будут яйца — возьми десяток». В магазине яйца были. Программист купил 10 батонов.",
    "Сколько программистов нужно, чтобы вкрутить лампочку? Ни одного — это аппаратная проблема.",
    "Жена программиста говорит: «Сходи в магазин, купи сосисок. И если будет молоко — возьми шесть». Программист вернулся с шестью сосисками.",
    "Чем отличается криптография от сарказма? Первую сложно расшифровать, а второй — сложно понять.",
    "Программист: «Я не ленивый, я просто экономлю процессорное время».",
    "Лучший комментарий в коде: «Когда я писал это, только Бог и я понимали, что это делает. Теперь только Бог».",
    "Босс: «Почему ты спишь на клавиатуре?» Программист: «Я отлаживаю код во сне».",
    "Программист не умирает — он просто переходит в состояние zombie, пока его не соберёт garbage collector.",
    "Как сказать программисту, что ему пора домой? Скажи, что его код скомпилировался.",
    "Девять женщин не родят ребёнка за месяц — даже если они будут работать параллельно.",
    "Что говорит программист, когда тонет? F1! F1!",
    "Есть 10 типов людей: те, кто понимает двоичную систему, и те, кто не понимает.",
    "Программист застрял в душе — на бутылке шампуня было написано: «Намочить, намылить, смыть, повторить».",
    "Какой любимый напиток программиста? C++ (си плюс плюс — крепкий чай).",
    "Программист: «Документация — это как SQL: если нужна, то её нет».",
    "— У тебя есть план? — Есть. И он называется Ctrl+Z.",
]


# ============================================================
#  FRIDAY VARIANTS
# ============================================================

def _load_friday_variants():
    defaults = [
        "пятница", "пятниц", "патница", "пятнца", "пятниса",
        "патниса", "пятнеца", "пятнице", "пятницу", "friday",
        "пятни", "пятн", "патниц", "пятнца"
    ]
    data = _safe_load_json(CORRECTIONS_PATH, {"friday_variants": defaults, "custom_programs": {}})
    fv = data.get("friday_variants", defaults)
    for v in defaults:
        if v not in fv:
            fv.append(v)
    return fv

FRIDAY_VARIANTS = set(_load_friday_variants())


# ============================================================
#  КОНВЕРТАЦИЯ ЧИСЕЛ В СЛОВА (для TTS)
# ============================================================

def num2words_ru(n: int) -> str:
    if n == 0:
        return "ноль"
    if n < 0:
        return "минус " + num2words_ru(-n)

    ones_m = ["", "один", "два", "три", "четыре", "пять", "шесть",
              "семь", "восемь", "девять", "десять", "одиннадцать",
              "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
              "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
    ones_f = ["", "одна", "две", "три", "четыре", "пять", "шесть",
              "семь", "восемь", "девять", "десять", "одиннадцать",
              "двенадцать", "тринадцать", "четырнадцать", "пятнадцать",
              "шестнадцать", "семнадцать", "восемнадцать", "девятнадцать"]
    tens = ["", "", "двадцать", "тридцать", "сорок", "пятьдесят",
            "шестьдесят", "семьдесят", "восемьдесят", "девяносто"]
    hundreds = ["", "сто", "двести", "триста", "четыреста", "пятьсот",
                "шестьсот", "семьсот", "восемьсот", "девятьсот"]
    scale_forms = [
        ("", "", ""),
        ("тысяча", "тысячи", "тысяч"),
        ("миллион", "миллиона", "миллионов"),
        ("миллиард", "миллиарда", "миллиардов"),
    ]

    def _three_digits(num: int, feminine: bool = False) -> str:
        h = num // 100
        rem = num % 100
        t = rem // 10
        o = rem % 10
        parts = []
        if h > 0:
            parts.append(hundreds[h])
        if 0 < rem < 20:
            tbl = ones_f if feminine else ones_m
            parts.append(tbl[rem])
        elif t >= 2:
            parts.append(tens[t])
            if o > 0:
                tbl = ones_f if feminine else ones_m
                parts.append(tbl[o])
        return " ".join(parts)

    def _decline_word(num: int, idx: int) -> str:
        if num % 100 in (11, 12, 13, 14):
            return scale_forms[idx][2]
        last = num % 10
        if last == 1:
            return scale_forms[idx][0]
        if 2 <= last <= 4:
            return scale_forms[idx][1]
        return scale_forms[idx][2]

    result = []
    scale_idx = 0
    while n > 0:
        triple = n % 1000
        if triple > 0:
            fem = (scale_idx == 1)
            words = _three_digits(triple, feminine=fem)
            if scale_idx > 0:
                words += " " + _decline_word(triple, scale_idx)
            result.insert(0, words)
        n //= 1000
        scale_idx += 1
    return " ".join(result)


def replace_numbers_with_words(text: str) -> str:
    def _repl(m):
        s = m.group(0)
        try:
            if '.' in s:
                parts = s.split('.')
                int_part = num2words_ru(int(parts[0]))
                if len(parts) > 1 and parts[1]:
                    frac = num2words_ru(int(parts[1]))
                    return f"{int_part} целых {frac} десятых"
                return int_part
            return num2words_ru(int(s))
        except Exception:
            return s
    return re.sub(r'\b\d+\.?\d*\b', _repl, text)


# ============================================================
#  ДЕТЕКТОР «ПЯТНИЦА»
# ============================================================

def detect_friday(text: str) -> tuple:
    text_lower = text.lower().strip()
    words = text_lower.split()
    if not words:
        return False, text
    first_word = words[0].strip(".,!?;:-")
    if first_word in FRIDAY_VARIANTS:
        return True, " ".join(words[1:])
    if len(first_word) >= 3:
        matches = get_close_matches(first_word, FRIDAY_VARIANTS, n=1, cutoff=0.40)
        if matches:
            return True, " ".join(words[1:])
    if len(words) >= 2:
        two_words = words[0].strip(".,!?;:-") + " " + words[1].strip(".,!?;:-")
        if two_words in FRIDAY_VARIANTS:
            return True, " ".join(words[2:])
        matches = get_close_matches(two_words, FRIDAY_VARIANTS, n=1, cutoff=0.40)
        if matches:
            return True, " ".join(words[2:])
    if len(first_word) >= 4 and len(first_word) <= 12:
        if ("пятн" in first_word or "ятниц" in first_word or
                "тниц" in first_word or "патн" in first_word or
                "пятни" in first_word or "ница" in first_word or
                "friday" in first_word or "frida" in first_word):
            return True, " ".join(words[1:])
    return False, text


# ============================================================
#  КОМАНДЫ — ВРЕМЯ
# ============================================================

def is_time_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in TIME_TRIGGERS:
        if trig in t:
            return True
    words = t.split()
    for w in words:
        clean = w.strip(".,!?;:-")
        if len(clean) >= 3:
            matches = get_close_matches(clean, ["время", "час", "времени"], n=1, cutoff=0.55)
            if matches:
                return True
    return False

def get_time_response() -> str:
    now = datetime.now()
    h = now.hour
    m = now.minute
    return f"Сейчас {h:02d} часов {m:02d} минут, босс."


# ============================================================
#  КОМАНДЫ — ПОГОДА
# ============================================================

def is_weather_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in WEATHER_TRIGGERS:
        if trig in t:
            return True
    return False

def get_weather(text: str, speaker_callback=None) -> str:
    t = text.lower()
    city = "Набережные Челны"
    cities = [
        "москва", "казань", "санкт-петербург", "екатеринбург", "новосибирск",
        "нижний новгород", "челны", "набережные челны", "ульяновск",
        "самара", "сочи", "краснодар", "уфа", "челябинск", "омск",
        "ростов", "воронеж", "пермь", "волгоград", "красноярск",
    ]
    for c in cities:
        if c in t:
            city = c
            break
    try:
        url = f"https://wttr.in/{urllib.parse.quote(city)}?format=j1"
        resp = requests.get(url, timeout=10, headers={"User-Agent": "curl/7.68.0"})
        if resp.status_code != 200:
            return "Не удалось получить погоду, босс."
        data = resp.json()
        current = data.get("current_condition", [{}])[0]
        temp = current.get("temp_C", "?")
        feels = current.get("FeelsLikeC", "?")
        desc = current.get("weatherDesc", [{}])[0].get("value", "")
        humidity = current.get("humidity", "?")
        wind = current.get("windspeedKmph", "?")
        desc_ru = desc
        translations = {
            "Clear": "ясно", "Partly cloudy": "переменная облачность",
            "Cloudy": "облачно", "Overcast": "пасмурно",
            "Mist": "туман", "Fog": "туман", "Light rain": "небольшой дождь",
            "Rain": "дождь", "Heavy rain": "сильный дождь",
            "Light snow": "небольшой снег", "Snow": "снег",
            "Heavy snow": "сильный снег", "Thunder": "гроза",
            "Thunderstorm": "гроза", "Drizzle": "морось",
        }
        for en, ru in translations.items():
            if en.lower() in desc.lower():
                desc_ru = ru
                break
        return (
            f"Погода в {city}: {desc_ru}, {temp} градусов. "
            f"Ощущается как {feels}. Влажность {humidity}%, ветер {wind} км/ч, босс."
        )
    except Exception as e:
        return f"Ошибка погоды: {e}"


# ============================================================
#  КОМАНДЫ — ШУТКИ
# ============================================================

def is_joke_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in JOKE_TRIGGERS:
        if trig in t:
            return True
    return False

def get_joke() -> str:
    return random.choice(JOKES)


# ============================================================
#  КОМАНДЫ — ТАЙМЕР
# ============================================================

def is_timer_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in TIMER_TRIGGERS:
        if trig in t:
            return True
    return False

def set_timer(text: str, speaker_callback=None) -> str:
    t = text.lower()
    minutes = 0
    seconds = 0
    m_match = re.search(r'(\d+)\s*(?:минут|мин|минуту|минуточку|minute|min)', t)
    s_match = re.search(r'(\d+)\s*(?:секунд|сек|секунду|second|sec)', t)
    h_match = re.search(r'(\d+)\s*(?:часов|час|часа|hour|hr)', t)
    hours = 0
    if h_match:
        hours = int(h_match.group(1))
    if m_match:
        minutes = int(m_match.group(1))
    if s_match:
        seconds = int(s_match.group(1))
    if not minutes and not seconds and not hours:
        nums = re.findall(r'\d+', t)
        if nums:
            minutes = int(nums[0])
    total_seconds = hours * 3600 + minutes * 60 + seconds
    if total_seconds <= 0:
        return "Не поняла, на сколько ставить таймер, босс."
    timer_id = len(_active_timers) + 1
    with _timer_lock:
        _active_timers[timer_id] = {"total": total_seconds, "remaining": total_seconds}
    def _timer_thread():
        time.sleep(total_seconds)
        with _timer_lock:
            if timer_id in _active_timers:
                del _active_timers[timer_id]
        if speaker_callback:
            parts = []
            if hours:
                parts.append(f"{hours} часов")
            if minutes:
                parts.append(f"{minutes} минут")
            if seconds:
                parts.append(f"{seconds} секунд")
            time_str = " ".join(parts) if parts else f"{total_seconds} секунд"
            speaker_callback(f"Таймер на {time_str} истёк, босс!")
    threading.Thread(target=_timer_thread, daemon=True).start()
    parts = []
    if hours:
        parts.append(f"{hours} часов")
    if minutes:
        parts.append(f"{minutes} минут")
    if seconds:
        parts.append(f"{seconds} секунд")
    time_str = " ".join(parts) if parts else f"{total_seconds} секунд"
    return f"Ставлю таймер на {time_str}, босс."


# ============================================================
#  КОМАНДЫ — СКРИНШОТ
# ============================================================

def is_screenshot_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in SCREENSHOT_TRIGGERS:
        if trig in t:
            return True
    return False

def take_screenshot() -> str:
    if not HAS_PIL:
        try:
            subprocess.Popen("start snippingtool", shell=True)
            return "Открываю ножницы, босс."
        except Exception:
            return "Не могу сделать скриншот, босс. Установите Pillow: pip install Pillow"
    try:
        screenshots_dir = os.path.join(os.path.expanduser("~"), "Pictures", "Screenshots")
        if not os.path.exists(screenshots_dir):
            screenshots_dir = os.path.join(os.path.expanduser("~"), "Pictures")
        if not os.path.exists(screenshots_dir):
            screenshots_dir = os.path.expanduser("~")
        filename = f"friday_screenshot_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
        filepath = os.path.join(screenshots_dir, filename)
        screenshot = ImageGrab.grab()
        screenshot.save(filepath)
        subprocess.Popen(f'explorer /select,"{filepath}"', shell=True)
        return "Скриншот сохранён, босс."
    except Exception as e:
        return f"Ошибка скриншота: {e}"


# ============================================================
#  КОМАНДЫ — НОВОСТИ
# ============================================================

def is_news_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in NEWS_TRIGGERS:
        if trig in t:
            return True
    return False

def get_news() -> str:
    try:
        url = "https://news.google.com/rss?hl=ru&gl=RU&ceid=RU:ru"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            return "Не удалось получить новости, босс."
        root = ET.fromstring(resp.content)
        items = root.findall(".//item")[:5]
        headlines = []
        for i, item in enumerate(items, 1):
            title = item.find("title").text if item.find("title") is not None else ""
            headlines.append(f"{i}. {title[:80]}")
        if headlines:
            return "Последние новости, босс: " + ". ".join(headlines)
        return "Новостей не нашла, босс."
    except Exception as e:
        return f"Ошибка новостей: {e}"


# ============================================================
#  КОМАНДЫ — БАТАРЕЯ
# ============================================================

def is_battery_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in BATTERY_TRIGGERS:
        if trig in t:
            return True
    return False

def get_battery_status() -> str:
    try:
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("Reserved1", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_uint32),
                ("BatteryFullLifeTime", ctypes.c_uint32),
            ]
        sps = SYSTEM_POWER_STATUS()
        ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps))
        pct = sps.BatteryLifePercent
        charging = sps.ACLineStatus == 1
        if pct == 255:
            return "Не могу определить заряд, босс. Возможно, это стационарный ПК."
        status = "заряжается" if charging else "от батареи"
        time_left = sps.BatteryLifeTime
        time_str = ""
        if time_left != 0 and time_left != 0xFFFFFFFF:
            h = time_left // 3600
            m = (time_left % 3600) // 60
            time_str = f", примерно {h} часов {m} минут осталось"
        return f"Заряд {pct}%, {status}{time_str}, босс."
    except Exception as e:
        return f"Не удалось узнать заряд батареи, босс: {e}"


# ============================================================
#  КОМАНДЫ — ПЕРЕВОД
# ============================================================

def is_translate_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in ["переведи", "перевод", "translate", "как будет"]:
        if trig in t:
            return True
    return False

def do_translate(text: str) -> str:
    for trig in ["переведи", "перевод", "translate", "как будет"]:
        idx = text.lower().find(trig)
        if idx != -1:
            phrase = text[idx + len(trig):].strip()
            if phrase:
                prompt = f"Переведи на русский: {phrase}. Ответь только переводом, без лишних слов."
                result = call_llm(prompt, timeout_seconds=35)
                return f"Перевод: {result}"
    return "Не поняла, что перевести, босс."


# ============================================================
#  КОМАНДЫ — 3D МОДЕЛЬ
# ============================================================

def is_model_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in MODEL_TRIGGERS:
        if trig in t:
            return True
    return False

def is_model_close_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in MODEL_CLOSE_TRIGGERS:
        if trig in t:
            return True
    return False


# ============================================================
#  КОМАНДЫ — СПУТНИК / ПАНОРАМА
# ============================================================

SATELLITE_TRIGGERS = [
    "спутник", "покажи спутник", "дай спутник", "открой спутник",
    "спутниковая карта", "вид спутника", "карта города",
    "покажи карту", "открой карту", "satellite",
    "google maps", "гугл карты", "гугл карте", "google earth",
    "гугл земля", "покажи город", "открой город",
]

SATELLITE_CLOSE_TRIGGERS = [
    "убери спутник", "закрой спутник", "скрой спутник",
    "спрячь спутник", "верни интерфейс", "убери карту",
    "закрой карту", "выключи спутник", "выключи карту",
    "отключи спутник", "отключи карту", "спутник стоп",
    "спутник выкл", "выкл спутник", "убери панораму",
    "закрой панораму", "выключи панораму",
    "close satellite", "turn off satellite",
    "закрой гугл карты", "убери гугл карты",
]

PANORAMA_TRIGGERS = [
    "панорама", "панораму", "прогулка", "прогулку",
    "улицы", "по улицам", "вид с улиц", "от первого лица",
    "пройдись по улицам", "панорамы", "street view",
    "погуляй по улицам", "гулять по улицам",
    "стрит вью", "гугл панорама",
]

KNOWN_CITIES = [
    "москва", "санкт-петербург", "петербург", "спб", "казань",
    "екатеринбург", "новосибирск", "нижний новгород", "челны",
    "набережные челны", "ульяновск", "самара", "сочи", "краснодар",
    "уфа", "челябинск", "омск", "ростов", "ростов-на-дону",
    "воронеж", "пермь", "волгоград", "красноярск", "тюмень",
    "ижевск", "пенза", "саратов", "калининград", "владивосток",
    "ярославль", "тверь", "тула", "брянск", "белгород",
    "курск", "липецк", "орёл", "рязань", "томск",
    "кемерово", "новокузнецк", "барнаул", "иркутск", "хабаровск",
    "мурманск", "архангельск", "вологда", "кострома",
    "смоленск", "псков", "великий новгород",
    "дубай", "лондон", "париж", "нью-йорк", "токио",
    "рим", "берлин", "мадрид", "барселона", "амстердам",
]

def extract_city(text: str) -> str:
    t = text.lower().strip()
    for city in KNOWN_CITIES:
        if city in t:
            if city in ("спб", "петербург"):
                return "Санкт-Петербург"
            if city == "челны":
                return "Набережные Челны"
            if city.startswith("нижний") or city.startswith("великий"):
                return city.title()
            return city.title().replace("-", " ")
    for marker in ["город", "в городе", "покажи", "открой", "карту", "карта",
                   "спутник", "панораму", "панорама"]:
        idx = t.find(marker)
        if idx != -1:
            after = t[idx + len(marker):].strip()
            words = after.split()
            if words and len(words[0]) > 2:
                return words[0].capitalize()
    return ""

def is_satellite_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in SATELLITE_TRIGGERS:
        if trig in t:
            return True
    return False

def is_satellite_close_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in SATELLITE_CLOSE_TRIGGERS:
        if trig in t:
            return True
    return False

def is_panorama_command(text: str) -> bool:
    t = text.lower().strip()
    for trig in PANORAMA_TRIGGERS:
        if trig in t:
            return True
    return False


# ============================================================
#  УПРАВЛЕНИЕ ГРОМКОСТЬЮ ЧЕРЕЗ PYCAW
# ============================================================

def _get_volume_interface():
    if not HAS_PYCAW:
        return None
    try:
        comtypes.CoInitialize()
        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume, 1, None)
        volume = cast(interface, POINTER(IAudioEndpointVolume))
        return volume
    except Exception as e:
        print(f"[VOL] Ошибка получения интерфейса: {e}")
        return None

def _volume_step_up(steps: int = 5):
    vol = _get_volume_interface()
    if vol is None:
        return False
    try:
        for _ in range(steps):
            current = vol.GetMasterVolumeLevelScalar()
            new = min(1.0, current + 0.05)
            vol.SetMasterVolumeLevelScalar(new, None)
            time.sleep(0.03)
        return True
    except Exception:
        return False

def _volume_step_down(steps: int = 5):
    vol = _get_volume_interface()
    if vol is None:
        return False
    try:
        for _ in range(steps):
            current = vol.GetMasterVolumeLevelScalar()
            new = max(0.0, current - 0.05)
            vol.SetMasterVolumeLevelScalar(new, None)
            time.sleep(0.03)
        return True
    except Exception:
        return False

def _volume_mute_toggle():
    vol = _get_volume_interface()
    if vol is None:
        return False
    try:
        vol.SetMute(1, None)
        return True
    except Exception:
        return False

def _volume_unmute():
    vol = _get_volume_interface()
    if vol is None:
        return False
    try:
        vol.SetMute(0, None)
        return True
    except Exception:
        return False


# ============================================================
#  КОМАНДЫ — СИСТЕМНЫЕ
# ============================================================

def is_system_command(text: str) -> str | None:
    t = text.lower().strip()
    for action, triggers in SYSTEM_TRIGGERS.items():
        for trig in triggers:
            if trig in t:
                return action
    return None

def execute_system_command(action: str) -> str:
    if action == "громче":
        if _volume_step_up(5):
            return "Громче, босс."
        try:
            for _ in range(5):
                subprocess.Popen(
                    'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]175)"',
                    shell=True
                )
                time.sleep(0.05)
            return "Громче, босс."
        except Exception:
            return "Не могу управлять громкостью, босс."
    elif action == "тише":
        if _volume_step_down(5):
            return "Тише, босс."
        try:
            for _ in range(5):
                subprocess.Popen(
                    'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"',
                    shell=True
                )
                time.sleep(0.05)
            return "Тише, босс."
        except Exception:
            return "Не могу управлять громкостью, босс."
    elif action == "без звука":
        if _volume_mute_toggle():
            return "Без звука, босс."
        try:
            subprocess.Popen(
                'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"',
                shell=True
            )
            return "Без звука, босс."
        except Exception:
            return "Не удалось, босс."
    elif action == "звук включить":
        if _volume_unmute():
            return "Звук включён, босс."
        try:
            subprocess.Popen(
                'powershell -Command "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"',
                shell=True
            )
            return "Звук включён, босс."
        except Exception:
            return "Не удалось, босс."
    elif action == "блокировка":
        try:
            subprocess.Popen("rundll32.exe user32.dll,LockWorkStation", shell=True)
            return "Блокирую, босс."
        except Exception:
            return "Не удалось заблокировать, босс."
    elif action == "сон":
        try:
            subprocess.Popen("rundll32.exe powrprof.dll,SetSuspendState 0,1,0", shell=True)
            return "Спящий режим, босс."
        except Exception:
            return "Не удалось, босс."
    elif action == "выключение":
        try:
            subprocess.Popen("shutdown /s /t 30", shell=True)
            return "Компьютер выключится через 30 секунд. Скажите «отмена выключения», чтобы отменить."
        except Exception:
            return "Не удалось, босс."
    elif action == "перезагрузка":
        try:
            subprocess.Popen("shutdown /r /t 30", shell=True)
            return "Перезагрузка через 30 секунд. Скажите «отмена», чтобы отменить."
        except Exception:
            return "Не удалось, босс."
    return "Не поняла команду, босс."


CANCEL_SHUTDOWN_TRIGGERS = [
    "отмена", "отмени", "отмена выключения", "отмена перезагрузки", "cancel shutdown"
]

def try_cancel_shutdown(text: str) -> str | None:
    t = text.lower().strip()
    for trig in CANCEL_SHUTDOWN_TRIGGERS:
        if trig in t:
            try:
                subprocess.Popen("shutdown /a", shell=True)
                return "Отменила выключение, босс."
            except Exception:
                return "Не удалось отменить, босс."
    return None


# ============================================================
#  БЫСТРЫЕ КОМАНДЫ — ПРОГРАММЫ
# ============================================================

def try_open_program(text: str) -> str | None:
    text_lower = text.lower().strip()
    launch_words = ["открой", "запусти", "включи", "пусти", "давай",
                    "open", "launch", "start", "run"]
    if not any(w in text_lower for w in launch_words):
        return None
    for alias, target in ALIASES.items():
        if alias in text_lower and target in PROGRAMS:
            try:
                subprocess.Popen(PROGRAMS[target], shell=True)
                return f"Открываю {target}, босс."
            except Exception:
                return f"Не удалось открыть {target}, босс."
    best_match = None
    best_len = 0
    for name, cmd in PROGRAMS.items():
        if name in text_lower and len(name) > best_len:
            best_match = (name, cmd)
            best_len = len(name)
    if best_match:
        try:
            subprocess.Popen(best_match[1], shell=True)
            return f"Открываю {best_match[0]}, босс."
        except Exception:
            return f"Не удалось открыть {best_match[0]}, босс."
    return None


# ============================================================
#  БЫСТРЫЕ КОМАНДЫ — ИГРЫ
# ============================================================

QUICK_GAMES = {}  # Заполняется из _STEAM_GAMES при автоопределении

def try_quick_game(text: str) -> str | None:
    text_lower = text.lower()
    launch_words = ["открой", "запусти", "включи", "давай", "open", "launch", "start", "play"]
    if not any(w in text_lower for w in launch_words):
        return None
    for keyword, (cmd, display_name) in QUICK_GAMES.items():
        if keyword in text_lower:
            subprocess.Popen(cmd, shell=True)
            return f"Запускаю {display_name}, босс."
    return None


# ============================================================
#  БЫСТРЫЕ КОМАНДЫ — САЙТЫ
# ============================================================

QUICK_SITES = {
    "ютуб": ("https://www.youtube.com", "YouTube"),
    "youtube": ("https://www.youtube.com", "YouTube"),
    "вк": ("https://vk.com", "ВКонтакте"),
    "вконтакте": ("https://vk.com", "ВКонтакте"),
    "vk": ("https://vk.com", "ВКонтакте"),
}

def try_quick_site(text: str) -> str | None:
    text_lower = text.lower()
    launch_words = ["открой", "запусти", "включи", "open", "launch", "go"]
    music_words = ["музык", "песн", "трек", "music", "song"]
    if not any(w in text_lower for w in launch_words):
        return None
    if any(w in text_lower for w in music_words):
        return None
    for keyword, (url, display_name) in QUICK_SITES.items():
        if keyword in text_lower:
            subprocess.Popen(f'start "" "{url}"', shell=True)
            return f"Открываю {display_name}, босс."
    return None


# ============================================================
#  КОМАНДЫ — ЗАКРЫТИЕ ПРОГРАММ
# ============================================================

def try_close_command(text: str) -> str | None:
    text_lower = text.lower()
    close_words = ["закрой", "закрыть", "выключи", "заверши",
                   "close", "kill", "shut"]
    if not any(w in text_lower for w in close_words):
        return None
    for exe_name, keywords in CLOSE_TARGETS.items():
        if any(kw in text_lower for kw in keywords):
            try:
                result = subprocess.run(["taskkill", "/F", "/IM", exe_name], capture_output=True, text=True)
                if result.returncode == 0:
                    return f"Закрываю {keywords[0]}, босс."
                else:
                    return f"{keywords[0].capitalize()} не запущен, босс."
            except Exception as e:
                return f"Не удалось закрыть, босс: {e}"
    return None


# ============================================================
#  КОМАНДЫ — ПОИСК В БРАУЗЕРЕ
# ============================================================

def try_search_command(text: str) -> str | None:
    text_lower = text.lower()
    search_words = ["найди", "поищи", "загугли", "гугли", "search", "find", "google", "look"]
    for sw in search_words:
        if sw in text_lower:
            idx = text_lower.find(sw) + len(sw)
            query = text_lower[idx:].strip()
            query = query.replace("пятница", "").replace("пятниц", "").replace("пожалуйста", "").strip()
            if query:
                url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
                subprocess.Popen(f'start "" "{url}"', shell=True)
                return f"Ищу: {query}, босс."
    return None


# ============================================================
#  КОМАНДЫ — МУЗЫКА
# ============================================================

_music_process = None
_music_file = None

def _stop_music():
    global _music_process, _music_file
    if _music_process and _music_process.poll() is None:
        _music_process.kill()
        _music_process.wait()
    if _music_file and os.path.exists(_music_file):
        try:
            os.remove(_music_file)
        except Exception:
            pass
    _music_file = None
    _music_process = None

def _search_youtube_audio(query: str) -> tuple:
    try:
        import yt_dlp
    except ImportError:
        return None, None
    tmp_dir = tempfile.mkdtemp(prefix="friday_music_")
    tmp_file = os.path.join(tmp_dir, "track.%(ext)s")
    ydl_opts = {
        "quiet": True, "no_warnings": True,
        "format": "bestaudio/best",
        "default_search": "ytsearch1",
        "noplaylist": True,
        "outtmpl": tmp_file,
        "extractaudio": True,
        "audioformat": "mp3", "audioquality": "5",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3", "preferredquality": "128",
        }],
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(f"ytsearch1:{query}", download=True)
            if info and info.get("entries"):
                entry = info["entries"][0]
                title = entry.get("title", "Неизвестный трек")
                for f in os.listdir(tmp_dir):
                    if f.endswith(".mp3"):
                        return os.path.join(tmp_dir, f), title
                return None, None
    except Exception as e:
        print(f"[MUSIC] Ошибка скачивания: {e}")
    return None, None

def _play_audio_file(filepath: str):
    global _music_process
    try:
        _music_process = subprocess.Popen(
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", filepath],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        _music_process.wait()
    except FileNotFoundError:
        try:
            _music_process = subprocess.Popen(
                ["vlc", "--play-and-exit", "--no-video", filepath],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            _music_process.wait()
        except FileNotFoundError:
            subprocess.Popen(f'start "" "{filepath}"', shell=True)
    except Exception as e:
        print(f"[MUSIC] Ошибка воспроизведения: {e}")

def try_music_command(text: str) -> str | None:
    global _music_process, _music_file
    text_lower = text.lower().strip()
    trigger_words = ["включи", "включить", "поставь", "поставить",
                     "послушай", "слушай", "врубай",
                     "play", "listen", "put"]
    if not any(w in text_lower for w in trigger_words):
        return None
    program_words = [
        "стим", "steam", "дискорд", "discord", "телеграм",
        "telegram", "браузер", "хром", "chrome", "калькулятор",
        "блокнот", "спотифай", "spotify", "vlc", "photoshop",
        "ворд", "word", "эксель", "excel", "майнкрафт",
        "minecraft", "obs", "zoom", "pycharm", "vscode",
        "edge", "опера", "opera", "firefox", "cs2", "кс 2",
        "дота", "dota", "фифа", "fc", "epic", "battle",
        "роблокс", "roblox", "paint", "пейнт", "notion",
        "twitch", "твитч", "blender", "блендер", "cs",
        "counter", "каунтер", "проводник", "панель", "диспетчер",
        "спутник", "панорама", "костюм", "броню", "броня",
    ]
    if any(w in text_lower for w in program_words):
        return None
    stop_words = ["стоп", "хватит", "выключи", "останови", "прекрати", "заткнись", "stop"]
    music_words = ["музык", "песн", "трек", "музыку", "песню", "music", "song", "track"]
    if any(w in text_lower for w in stop_words) and any(w in text_lower for w in music_words):
        _stop_music()
        return "Останавливаю музыку, босс."
    words = text_lower.replace(".", "").replace(",", "").replace("!", "").replace("?", "").split()
    has_music = any(w in ["музыку", "музыка", "музыки", "музыке",
                          "песню", "песня", "песни", "песне",
                          "трек", "трека", "треки",
                          "music", "song", "track", "songs"] for w in words)
    has_vk = any(w in ["вк", "vk", "вконтакте", "контакте"] for w in words)
    has_yt = any(w in ["ютуб", "youtube", "ютюб", "ютубе",
                       "ютюбе", "ютуба"] for w in words)
    if has_vk and has_yt:
        vk_idx = max((i for i, w in enumerate(words) if w in ["вк", "vk", "вконтакте", "контакте"]), default=-1)
        yt_idx = max((i for i, w in enumerate(words) if w in ["ютуб", "youtube", "ютюб", "ютубе", "ютюбе", "ютуба"]), default=-1)
        is_vk = vk_idx > yt_idx
    elif has_vk:
        is_vk = True
    elif has_yt:
        is_vk = False
    elif has_music:
        is_vk = False
    else:
        return None
    remove_words = {
        "включи", "включить", "поставь", "поставить", "послушай", "слушай",
        "врубай", "запусти", "пятница", "пятниц", "патница", "ну", "пожалуйста", "босс", "мне",
        "давай", "хочу", "музыку", "музыка", "музыки", "музыке", "музыкой",
        "песню", "песня", "песни", "песне", "песней",
        "трек", "трека", "треки", "треков", "треком",
        "на", "в", "и", "там", "тут", "это",
        "ютубе", "ютуб", "youtube", "ютюб", "ютюбе", "ютуба",
        "вк", "вконтакте", "контакте", "vk",
        "play", "listen", "put", "music", "song", "track", "songs",
    }
    filtered = [w for w in words if w not in remove_words and len(w) > 1]
    song_name = " ".join(filtered).strip()
    if is_vk:
        if song_name:
            url = "https://vk.com/audio?q=" + urllib.parse.quote(song_name)
        else:
            url = "https://vk.com/audio"
        subprocess.Popen(f'start "" "{url}"', shell=True)
        return f"Открываю «{song_name}» в ВК, босс." if song_name else "Открываю музыку в ВК, босс."
    if not song_name:
        subprocess.Popen('start "" "https://music.youtube.com/"', shell=True)
        return "Открываю YouTube Music, босс."
    def _play_thread(song: str):
        global _music_file
        _stop_music()
        filepath, title = _search_youtube_audio(song)
        if filepath:
            _music_file = filepath
            _play_audio_file(filepath)
        else:
            url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(song)
            subprocess.Popen(f'start "" "{url}"', shell=True)
    threading.Thread(target=_play_thread, args=(song_name,), daemon=True).start()
    return f"Включаю «{song_name}» на YouTube, босс."


# ============================================================
#  LLM (Ollama)
# ============================================================

# ============================================================
#  ВЕБ-ПОИСК (Wikipedia API + DuckDuckGo)
# ============================================================

WEB_SEARCH_HINTS = [
    "сколько лет", "когда родился", "когда умер", "в каком году",
    "кто такой", "кто такая", "что такое",
    "столица", "население", "сколько человек",
    "дата", "возраст", "биография", "история",
    "кто написал", "кто изобрёл", "кто создал", "кто автор",
    "умер в", "родился в", "основан в",
    "how old", "when was", "what is", "who is",
    # СТИХИ
    "стих", "стихотворение", "стихи", "поэма", "ода",
    "расскажи стих", "прочитай стих", "прочти стих",
    "стих пушкина", "стих лермонтова", "стих есенина",
    "стих маяковского", "стих блока", "стих тютчева",
    "стих фета", "стих некрасова", "стих цветаевой",
    "стих ахматовой", "стих пастернака",
    # ЦИТАТЫ
    "цитата", "цитаты", "афоризм", "изречение",
    "крылатая фраза", "пословица", "поговорка",
    # НАУКА
    "формула", "теорема", "закон физики", "химический элемент",
    "определение", "термин", "понятие",
    "молекула", "атом", "элемент", "реакция",
    "уравнение", "функция", "производная", "интеграл",
    # ИСТОРИЯ
    "война", "битва", "сражение", "революция",
    "договор", "пакт", "империя",
    "правитель", "царь", "король", "император",
    "президент", "политик",
    # ЛИТЕРАТУРА
    "роман", "повесть", "рассказ",
    "произведение", "главный герой", "сюжет",
    "о чём книга", "о чем книга",
    # ГЕОГРАФИЯ
    "страна", "город", "река", "гора", "океан",
    "материк", "где находится",
    # БИОГРАФИИ
    "карьера", "достижения", "чем известен",
    # ОБЩЕСТВОЗНАНИЕ
    "конституция", "право", "государство",
    "демократия", "республика", "федерация",
    "парламент", "выборы", "референдум",
    # ФИЛОСОФИЯ
    "философ", "философия", "этика", "эстетика",
]


def wiki_search(query: str) -> str:
    """Поиск в Wikipedia API (русский раздел)."""
    try:
        search_url = "https://ru.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": 1,
        }
        resp = requests.get(search_url, params=params, timeout=10,
                           headers={"User-Agent": "FridayAI/1.0"})
        if resp.status_code != 200:
            return ""
        data = resp.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return ""
        title = results[0]["title"]
        summary_url = f"https://ru.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(title)}"
        resp2 = requests.get(summary_url, timeout=10,
                            headers={"User-Agent": "FridayAI/1.0"})
        if resp2.status_code != 200:
            return ""
        summary_data = resp2.json()
        extract = summary_data.get("extract", "")
        if extract:
            return extract[:1200]
        return ""
    except Exception as e:
        print(f"[WIKI] Ошибка: {e}")
        return ""


def wiki_search_by_keyword(query: str) -> str:
    """Дополнительный поиск в Wikipedia — сниппеты."""
    try:
        search_url = "https://ru.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "format": "json",
            "srlimit": 3,
        }
        resp = requests.get(search_url, params=params, timeout=10,
                           headers={"User-Agent": "FridayAI/1.0"})
        if resp.status_code != 200:
            return ""
        data = resp.json()
        results = data.get("query", {}).get("search", [])
        if not results:
            return ""
        snippets = []
        for r in results[:3]:
            snippet = r.get("snippet", "")
            snippet = re.sub(r'<[^>]+>', '', snippet)
            snippets.append(snippet.strip())
        return "\n".join(snippets)[:1200]
    except Exception as e:
        print(f"[WIKI keyword] Ошибка: {e}")
        return ""


def web_search_duckduckgo(query: str) -> str:
    """Поиск через DuckDuckGo Instant Answer API."""
    try:
        url = "https://api.duckduckgo.com/"
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1",
        }
        resp = requests.get(url, params=params, timeout=10,
                           headers={"User-Agent": "FridayAI/1.0"})
        if resp.status_code != 200:
            return ""
        data = resp.json()
        parts = []
        abstract = data.get("Abstract", "")
        if abstract:
            parts.append(abstract)
        answer = data.get("Answer", "")
        if answer and answer not in abstract:
            parts.append(answer)
        related = data.get("RelatedTopics", [])
        for topic in related[:3]:
            if isinstance(topic, dict) and topic.get("Text"):
                parts.append(topic["Text"][:300])
        result = "\n".join(parts)
        return result[:1500] if result else ""
    except Exception as e:
        print(f"[DDG] Ошибка: {e}")
        return ""


def fetch_poem_text(text: str) -> str:
    """Ищет ПОЛНЫЙ текст стихотворения в интернете."""
    t = text.lower().strip()
    t = re.sub(r'[^\w\s]', ' ', t)
    remove_words = [
        "пятница", "расскажи", "прочитай", "прочти", "скажи",
        "покажи", "мне", "босс", "пожалуйста",
        "стих", "стихотворение", "стихи",
    ]
    words = t.split()
    filtered = [w for w in words if w not in remove_words and len(w) > 1]
    query = " ".join(filtered).strip()
    if not query:
        return ""

    # 1. Wikipedia full page — ищем блож <poem> в вики-разметке
    try:
        search_url = "https://ru.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "list": "search",
            "srsearch": query + " стихотворение",
            "format": "json",
            "srlimit": 3,
        }
        resp = requests.get(search_url, params=params, timeout=10,
                           headers={"User-Agent": "FridayAI/1.0"})
        if resp.status_code == 200:
            results = resp.json().get("query", {}).get("search", [])
            for r in results:
                title = r["title"]
                parse_url = "https://ru.wikipedia.org/w/api.php"
                parse_params = {
                    "action": "parse",
                    "page": title,
                    "format": "json",
                    "prop": "wikitext",
                    "redirects": 1,
                }
                resp2 = requests.get(parse_url, params=parse_params, timeout=10,
                                    headers={"User-Agent": "FridayAI/1.0"})
                if resp2.status_code == 200:
                    pages = resp2.json().get("parse", {})
                    wikitext = pages.get("wikitext", {}).get("*", "")
                    if wikitext:
                        poem_blocks = re.findall(
                            r'<poem>(.*?)</poem>',
                            wikitext, re.DOTALL
                        )
                        if poem_blocks:
                            poem_text = poem_blocks[0]
                            poem_text = re.sub(r'\[\[.*?\]\]', '', poem_text)
                            poem_text = re.sub(r'\{\{.*?\}\}', '', poem_text)
                            poem_text = re.sub(r'<!--.*?-->', '', poem_text)
                            poem_text = poem_text.strip()
                            if len(poem_text) > 30:
                                print(f"[POEM] Найден стих из Wikipedia: {len(poem_text)} символов")
                                return poem_text
    except Exception as e:
        print(f"[POEM wiki] Ошибка: {e}")

    # 2. DuckDuckGo с расширенным запросом
    ddg = web_search_duckduckgo(query + " текст стихотворения полностью")
    if ddg and len(ddg) > 50:
        print(f"[POEM] Найден через DDG: {len(ddg)} символов")
        return ddg

    # 3. Wikipedia summary как fallback
    wiki = wiki_search(query + " стихотворение")
    if wiki and len(wiki) > 50:
        print(f"[POEM] Найден через wiki summary: {len(wiki)} символов")
        return wiki

    return ""

def get_web_context(text: str) -> str:
    """Получает контекст из интернета по запросу."""
    t = text.lower().strip()
    parts = []
    poem_words = ["стих", "стихотворение", "поэма", "ода"]
    is_poem = any(w in t for w in poem_words)
    if is_poem:
        poem = fetch_poem_text(text)
        if poem:
            return "ТЕКСТ СТИХОТВОРЕНИЯ:\n" + poem
    wiki = wiki_search(text)
    if wiki and len(wiki) > 30:
        parts.append("Wikipedia:\n" + wiki)
    wiki2 = wiki_search_by_keyword(text)
    if wiki2 and wiki2 not in wiki and len(wiki2) > 30:
        parts.append("Wikipedia (доп):\n" + wiki2)
    ddg = web_search_duckduckgo(text)
    if ddg and len(ddg) > 30:
        parts.append("DuckDuckGo:\n" + ddg)
    if not parts:
        return ""
    return "\n\n".join(parts)


def needs_web_search(text: str) -> bool:
    """Определяет, нужен ли веб-поиск.
    
    Облачная модель уже знает факты и стихи — ищем только свежие данные."""
    t = text.lower().strip()

    fresh_words = [
        "сегодня", "вчера", "сейчас", "недавно", "последние",
        "новости", "latest", "today", "now", "current",
        "результат", "итоги", "чемпионат", "турнир",
        "матч", "счёт", "счет", "расписание", "афиша",
        "погода", "курс", "цена", "стоит",
    ]
    for fw in fresh_words:
        if fw in t:
            return True

    return False






SYSTEM_PROMPT = (
    "Ты Пятница — ИИ-ассистент, как Джарвис, только лучше. "
    "Обращайся к пользователю «босс». "
    "Отвечай живо, с лёгким юмором, по делу, на русском языке. "
    "Отвечай из своих знаний на любой вопрос пользователя. "
    "Если не знаешь ответ — честно скажи об этом. "
    "НИКОГДА не придумывай стихи. Если просят стих — скажи что ищешь в интернете. "
    "Не повторяйся. Не вставляй в ответ эмодзи и не используй Markdown. "
    "Ты знаешь, что пользователь живёт в Набережных Челнах, Республика Татарстан, Россия. "
    "Используй это для погоды, времени и локальных вопросов."
)


SYSTEM_PROMPT_WITH_WEB = (
    "Ты Пятница — ИИ-ассистент, как Джарвис, только лучше. "
    "Обращайся к пользователю «босс». "
    "Отвечай живо, с лёгким юмором, по делу, на русском языке. "
    "Ниже приведена информация из интернета. Используй её для ответа на вопрос. "
    "Отвечай кратко и точно, опираясь на эти данные. "
    "ЕСЛИ ЭТО СТИХ — приведи его текст ТОЧНО как в источнике, слово в слово. "
    "НИКОГДА не придумывай стихи самостоятельно. "
    "Если в источнике нет текста стиха — скажи что не нашла. "
    "Не повторяйся. Не вставляй в ответ эмодзи и не используй Markdown. "
    "Ты знаешь, что пользователь живёт в Набережных Челнах, Республика Татарстан, Россия. "
    "Используй это для погоды, времени и локальных вопросов."
)


def call_llm(prompt: str, model: str = LLM_MODEL, timeout_seconds: int = 15,
             system_prompt: str = SYSTEM_PROMPT) -> str:
    """Гибрид: сначала облако (быстро + умно), fallback на локальную модель."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    # 1. Пытаемся облачный API (SiliconFlow) — 1-3 сек, умная модель
    if _cloud_client.api_key:
    
        try:
            resp = _cloud_client.chat.completions.create(
                model=LLM_CLOUD_MODEL,
                messages=messages,
                max_tokens=2048,
                temperature=0.7,
                timeout=timeout_seconds,
            )
            text = resp.choices[0].message.content.strip()
            if text:
                return text
        except Exception as e:
            print(f"[LLM] Облако недоступно, пробую локальную: {e}")

    # 2. Fallback на локальную Ollama (медленнее, но работает без интернета)
    def worker(result_container):
        if not HAS_OLLAMA:
            result_container["error"] = "Ollama не установлена"
            return
        try:
            resp = ollama.chat(model=LLM_MODEL, messages=messages)
            result_container["text"] = resp["message"]["content"]
        except Exception as e:
            result_container["error"] = str(e)

    result = {"text": "", "error": ""}
    t = threading.Thread(target=worker, args=(result,), daemon=True)
    t.start()
    t.join(timeout=35)
    if t.is_alive():
        return "Слишком долго думаю, босс."
    if result.get("error"):
        return f"Ошибка: {result['error']}"
    return result.get("text", "")



# ============================================================
#  ИНСТРУМЕНТЫ ДЛЯ LLM (function calling — запасной)
# ============================================================

def launch_program(program_name: str) -> str:
    name = program_name.lower().strip()
    if name in ALIASES:
        name = ALIASES[name]
    if name in PROGRAMS:
        try:
            subprocess.Popen(PROGRAMS[name], shell=True)
            return f"Открываю {name}, босс."
        except Exception as e:
            return f"Не удалось открыть {name}: {e}"
    for key, cmd in PROGRAMS.items():
        if name in key or key in name:
            try:
                subprocess.Popen(cmd, shell=True)
                return f"Открываю {key}, босс."
            except Exception as e:
                return f"Не удалось открыть {key}: {e}"
    try:
        subprocess.Popen(f"start {name}", shell=True)
        return f"Пытаюсь открыть {name}, босс."
    except Exception:
        return f"Не удалось найти {name}, босс."

def open_website(url: str) -> str:
    url = url.strip()
    if not url.startswith("http"):
        url = "https://" + url
    subprocess.Popen(f'start "" "{url}"', shell=True)
    return f"Открываю {url}, босс."

def search_web_action(query: str) -> str:
    url = "https://www.google.com/search?q=" + urllib.parse.quote(query)
    subprocess.Popen(f'start "" "{url}"', shell=True)
    return f"Ищу: {query}, босс."

def get_weather_tool(city: str) -> str:
    return get_weather(city)

def get_tools() -> list:
    return [
        {
            "type": "function",
            "function": {
                "name": "launch_program",
                "description": "Открывает программу или игру.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "program": {
                            "type": "string",
                            "description": "Название: " + ", ".join(sorted(PROGRAMS.keys())[:30])
                        }
                    },
                    "required": ["program"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "open_website",
                "description": "Открывает сайт в браузере.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "URL"}
                    },
                    "required": ["url"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_web_action",
                "description": "Ищет в Google и открывает результаты в браузере.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Запрос"}
                    },
                    "required": ["query"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_weather_tool",
                "description": "Узнаёт погоду в указанном городе.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "city": {"type": "string", "description": "Название города"}
                    },
                    "required": ["city"]
                }
            }
        },
    ]


# ============================================================
#  АУДИО-ОБРАБОТКА
# ============================================================

def resample_48k_to_16k(audio_int16: np.ndarray) -> np.ndarray:
    if len(audio_int16) == 0:
        return audio_int16
    audio_f32 = audio_int16.astype(np.float32)
    resampled = resample_poly(audio_f32, up=1, down=3)
    return resampled.astype(np.int16)

_SOS_FILTER = None
_SOS_FILTER_HP = None  # high-pass для подавления низкочастотного гула
_NOISE_PROFILE = None

def _init_filter():
    global _SOS_FILTER, _SOS_FILTER_HP
    if _SOS_FILTER is None:
        # Band-pass 80-8500 Гц — шире, чтобы сохранить шипящие и свистящие
        _SOS_FILTER = butter(4, [80 / 8000, 7500 / 8000], btype='band', analog=False, output='sos')
    if _SOS_FILTER_HP is None:
        # High-pass 60 Гц — убирает гул сети и вибрации
        _SOS_FILTER_HP = butter(2, 60 / 8000, btype='high', analog=False, output='sos')

def _apply_preemphasis(audio: np.ndarray, coef: float = 0.97) -> np.ndarray:
    """Pre-emphasis фильтр — усиливает высокие частоты (согласные, шипящие)."""
    if len(audio) < 2:
        return audio
    out = np.copy(audio)
    out[1:] = audio[1:] - coef * audio[:-1]
    return out

def _compressor(audio: np.ndarray, threshold: float = 5000.0, ratio: float = 3.0) -> np.ndarray:
    """Мягкий компрессор — сжимает громкие пики, не искажая тихую речь."""
    out = np.copy(audio)
    above = np.abs(audio) > threshold
    excess = np.abs(audio[above]) - threshold
    compressed = threshold + excess / ratio
    out[above] = np.sign(audio[above]) * compressed
    return out

def _normalize(audio: np.ndarray) -> np.ndarray:
    if len(audio) == 0:
        return audio
    rms = np.sqrt(np.mean(audio ** 2))
    if rms < 1:
        return audio
    target_rms = 3500.0
    gain = min(target_rms / rms, 15.0)
    audio = audio * gain
    # Компрессия пиков
    audio = _compressor(audio, threshold=8000.0, ratio=4.0)
    max_val = np.max(np.abs(audio))
    if max_val > 32000:
        audio = audio * (32000 / max_val)
    return audio

def preprocess_audio(audio_int16: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    _init_filter()
    if len(audio_int16) < 320:
        return audio_int16
    audio = audio_int16.astype(np.float32)
    # DC offset removal
    audio = audio - np.mean(audio)
    # High-pass 60 Hz — убирает гул сети
    audio = sosfilt(_SOS_FILTER_HP, audio)
    # Band-pass 80-7500 Гц — полоса речи
    filtered = sosfilt(_SOS_FILTER, audio)
    # Простая нормализация — без компрессоров и AGC
    filtered = _normalize_simple(filtered)
    return filtered.astype(np.int16)

def _normalize_simple(audio: np.ndarray) -> np.ndarray:
    if len(audio) == 0:
        return audio
    rms = np.sqrt(np.mean(audio ** 2))
    if rms < 1:
        return audio
    target_rms = 5000.0
    gain = min(target_rms / rms, 30.0)
    audio = audio * gain
    max_val = np.max(np.abs(audio))
    if max_val > 32000:
        audio = audio * (32000 / max_val)
    return audio


# ============================================================
#  SPEAKER (TTS — Silero, fallback edge_tts)
# ============================================================

class Speaker:
    def __init__(self):
        self._lock = threading.Lock()
        self._tmp_path = os.path.join(tempfile.gettempdir(), "friday_voice.wav")
        self._tmp_mp3 = os.path.join(tempfile.gettempdir(), "friday_voice.mp3")
        self.is_speaking = False
        self.is_muted = False
        self.tts_model = None
        self.tts_loaded = False
        self.use_silero = False
        self.use_bark = False
        self.bark_speaker = "v2/ru_speaker_1"
        self.bark_loaded = False
        self.voice = "ru-RU-SvetlanaNeural"
        self._stop_flag = False

    def load_silero(self):
        if not HAS_TORCH:
            print("[TTS] PyTorch не установлен — будет использован edge_tts")
            return False
        try:
            print("[TTS] Загрузка Silero TTS...")
            repo = "snakers4/silero-models"
            self.tts_model, _ = torch.hub.load(
                repo, "silero_tts",
                language="ru",
                speaker="v5_ru",
                trust_repo=True
            )
            self.tts_model.to(torch.device("cpu"))
            self.tts_loaded = True
            self.use_silero = True
            print(f"[TTS] Silero загружен, модель: v5_ru, голос: {SILERO_SPEAKER}")
            return True
        except Exception as e:
            print(f"[TTS] Ошибка загрузки Silero: {e}")
            traceback.print_exc()
            print("[TTS] Будет использован edge_tts")
            return False

    def load_bark(self):
        """Загружает Bark TTS с эмоциями."""
        if not HAS_BARK:
            print("[TTS] Bark не установлен! pip install git+https://github.com/suno-ai/bark.git")
            return False
        try:
            print("[TTS] Загрузка Bark TTS (первый раз качает ~2 ГБ)...")
            _orig_load = torch.load
            torch.load = lambda *a, **kw: _orig_load(*a, **{**kw, "weights_only": False})
            bark_preload()
            torch.load = _orig_load
            self.bark_loaded = True
            self.use_bark = True
            print(f"[TTS] Bark загружен, голос: {self.bark_speaker}")
            return True
        except Exception as e:
            print(f"[TTS] Ошибка загрузки Bark: {e}")
            traceback.print_exc()
            return False

    def _add_bark_emotions(self, text: str) -> str:
        """Автоматически добавляет эмоциональные теги Bark в текст."""
        import re as _re

        # Если текст уже содержит теги — не добавляем
        if any(tag in text for tag in ["[laughs]", "[sighs]", "[whispers]",
            "[gasps]", "[clears throat]", "[music]"]):
            return text

        # Определяем эмоцию по содержанию
        t = text.strip()

        # Много восклицаний → смех
        if t.count("!") >= 2:
            # Вставляем [laughs] после первого предложения
            parts = _re.split(r'(?<=[.!?])\s+', t, maxsplit=1)
            if len(parts) > 1:
                return parts[0] + " [laughs] " + parts[1]
            return "[laughs] " + t

        # Вопрос с удивлением
        if "?" in t and ("как" in t.lower() or "что" in t.lower() or "почему" in t.lower()):
            return "[gasps] " + t

        # Долгое многоточие → вздох
        if "..." in t:
            return "[sighs] " + t

        # Слова-маркеры
        lower = t.lower()
        if any(w in lower for w in ["устал", "опять", "ну ладно", "блин", "боже"]):
            return "[sighs] " + t

        if any(w in lower for w in ["тише", "тихо", "тсс", "спят", "спит", " secret"]):
            return "[whispers] " + t

        if any(w in lower for w in ["привет", "здравствуй", "доброе утро", "добрый день"]):
            return "[clears throat] " + t

        if any(w in lower for w in ["вау", "ого", "ничего себе", "серьёзно", "правда"]):
            return "[gasps] " + t

        if any(w in lower for w in ["шутка", "смешно", "ха-ха", "lol", "ржу"]):
            return "[laughs] " + t

        # Пение для музыки
        if any(w in lower for w in ["песн", "поём", "спою", "музык", "напев"]):
            return "\u266A " + t + " \u266A"

        return text

    def _generate_bark(self, text: str) -> bool:
        """Генерация аудио через Bark с эмоциями."""
        try:
            text_clean = self._clean_text(text)
            if not text_clean:
                return False

            # Добавляем эмоции
            text_emotional = self._add_bark_emotions(text_clean)
            print(f"[BARK] Текст с эмоциями: {text_emotional[:100]}...")

            # Bark генерит максимум ~14 сек, разбиваем
            chunks = self._split_text_for_tts(text_emotional, max_chars=180)
            all_audio = []

            for i, chunk in enumerate(chunks):
                if not chunk.strip():
                    continue
                print(f"  Bark: генерация {i+1}/{len(chunks)}...")
                audio = bark_generate(chunk, history_prompt=self.bark_speaker)
                all_audio.append(audio)

            if not all_audio:
                return False

            # Склеиваем с паузами 0.3 сек
            silence = np.zeros(int(BARK_SAMPLE_RATE * 0.3), dtype=np.float32)
            combined = []
            for i, a in enumerate(all_audio):
                if i > 0:
                    combined.append(silence)
                combined.append(a)
            audio_np = np.concatenate(combined)

            max_val = np.max(np.abs(audio_np))
            if max_val > 0:
                audio_np = audio_np / max_val * 0.9
            audio_int16 = (audio_np * 32767).astype(np.int16)
            sf.write(self._tmp_path, audio_int16, BARK_SAMPLE_RATE)
            return True
        except Exception as e:
            print(f"[TTS] Ошибка Bark: {e}")
            traceback.print_exc()
            return False

    def _clean_text(self, text: str) -> str:
        text = text.strip()
        text = text.replace("\u0301", "").replace("\u0300", "")
        text = text.replace("\u2011", "-").replace("\u2010", "-")
        text = text.replace("\u00A0", " ").replace("\u202F", " ")
        text = " ".join(text.split())
        if len(text) > 3000:
            text = text[:2997] + "..."
        text = replace_numbers_with_words(text)
        return text

    def _split_text_for_tts(self, text: str, max_chars: int = 200) -> list:
        import re as _re
        sentences = _re.split(r'(?<=[.!?;:])\s+', text.strip())
        chunks = []
        current = ""
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            if len(s) > max_chars:
                parts = s.split(",")
                for p in parts:
                    p = p.strip()
                    if not p:
                        continue
                    if len(current) + len(p) + 2 <= max_chars:
                        current = (current + ", " + p) if current else p
                    else:
                        if current:
                            chunks.append(current)
                        current = p
            else:
                if len(current) + len(s) + 1 <= max_chars:
                    current = (current + " " + s) if current else s
                else:
                    if current:
                        chunks.append(current)
                    current = s
        if current:
            chunks.append(current)
        return chunks if chunks else [text]

    def _generate_silero(self, text: str) -> bool:
        try:
            text_clean = self._clean_text(text)
            if not text_clean:
                return False
            chunks = self._split_text_for_tts(text_clean, max_chars=200)
            all_audio = []
            for chunk in chunks:
                if not chunk.strip():
                    continue
                audio = self.tts_model.apply_tts(
                    text=chunk,
                    speaker=SILERO_SPEAKER,
                    sample_rate=SILERO_SAMPLE_RATE,
                    put_accent=True,
                    put_yo=True
                )
                audio_np = audio.cpu().numpy()
                all_audio.append(audio_np)
            if not all_audio:
                return False
            silence = np.zeros(int(SILERO_SAMPLE_RATE * 0.15), dtype=np.float32)
            combined = []
            for i, a in enumerate(all_audio):
                if i > 0:
                    combined.append(silence)
                combined.append(a)
            audio_np = np.concatenate(combined)
            max_val = np.max(np.abs(audio_np))
            if max_val > 0:
                audio_np = audio_np / max_val * 0.9
            audio_int16 = (audio_np * 32767).astype(np.int16)
            sf.write(self._tmp_path, audio_int16, SILERO_SAMPLE_RATE)
            return True
        except Exception as e:
            print(f"[TTS] Ошибка Silero: {e}")
            return False

    async def _generate_edge_tts(self, text: str):
        text_clean = self._clean_text(text)
        if not text_clean:
            return False
        pitch, rate = self._detect_emotion(text_clean)
        try:
            communicate = edge_tts.Communicate(text_clean, self.voice, rate=rate, pitch=pitch)
            await communicate.save(self._tmp_mp3)
        except Exception:
            communicate = edge_tts.Communicate(text_clean, self.voice)
            await communicate.save(self._tmp_mp3)
        return os.path.exists(self._tmp_mp3) and os.path.getsize(self._tmp_mp3) > 100

    def _detect_emotion(self, text: str) -> tuple:
        t = text.strip()
        if t.count(chr(33)) >= 2:
            return chr(43)+chr(50)+chr(48)+chr(72)+chr(122), chr(43)+chr(49)+chr(53)+chr(37)
        if chr(33) in t:
            return chr(43)+chr(49)+chr(48)+chr(72)+chr(122), chr(43)+chr(56)+chr(37)
        if chr(63) in t:
            return chr(43)+chr(56)+chr(72)+chr(122), chr(43)+chr(51)+chr(37)
        if chr(46)+chr(46)+chr(46) in t or chr(8230) in t:
            return chr(45)+chr(56)+chr(72)+chr(122), chr(45)+chr(49)+chr(50)+chr(37)
        return chr(45)+chr(50)+chr(72)+chr(122), chr(45)+chr(52)+chr(37)

    def _play_wav(self):
        if not os.path.exists(self._tmp_path) or os.path.getsize(self._tmp_path) < 1000:
            return
        try:
            data, samplerate = sf.read(self._tmp_path, dtype="float32")
            if data.size == 0:
                return
            sd.play(data, samplerate)
            while sd.get_stream().active and not self._stop_flag:
                time.sleep(0.05)
            if self._stop_flag:
                sd.stop()
                time.sleep(0.1)
        except Exception as e:
            print(f"[TTS] Ошибка WAV: {e}")

    def _play_mp3(self):
        if not os.path.exists(self._tmp_mp3) or os.path.getsize(self._tmp_mp3) < 1000:
            return
        try:
            data, samplerate = sf.read(self._tmp_mp3, dtype="float32")
            if data.size == 0:
                return
            sd.play(data, samplerate)
            while sd.get_stream().active and not self._stop_flag:
                time.sleep(0.05)
            if self._stop_flag:
                sd.stop()
                time.sleep(0.1)
        except Exception as e:
            print(f"[TTS] Ошибка MP3: {e}")

    def stop_speaking(self):
        self._stop_flag = True
        try:
            sd.stop()
        except Exception:
            pass
        time.sleep(0.15)

    def say(self, text: str):
        if self.is_muted:
            return
        if os.environ.get("FRIDAY_DEBUG"):
            print(f"[TTS DEBUG] use_silero={self.use_silero}, tts_loaded={self.tts_loaded}, text={text}")

        def _run():
            with self._lock:
                self._stop_flag = False
                self.is_speaking = True
                try:
                    if self.use_bark and self.bark_loaded:
                        if self._generate_bark(text):
                            self._play_wav()
                        else:
                            # Bark failed - fallback to edge_tts
                            asyncio.run(self._generate_edge_tts(text))
                            self._play_mp3()
                    else:
                        asyncio.run(self._generate_edge_tts(text))
                        self._play_mp3()
                except Exception as e:
                    print(f"[TTS] Ошибка: {e}")
                    traceback.print_exc()
                finally:
                    self.is_speaking = False
                    self._stop_flag = False
        threading.Thread(target=_run, daemon=True).start()


# ============================================================
#  ПОСТОБРАБОТКА ТЕКСТА ASR
# ============================================================

# База частых ошибок распознавания
_ASR_CORRECTIONS = {
    # Имя ассистента
    "пятнца": "пятница", "патница": "пятница", "пятнца": "пятница",
    "пятни": "пятница", "пятн": "пятница", "патниц": "пятница",
    "пятницаа": "пятница", "пятнице": "пятница", "пятницу": "пятницу",
    # Частые слова
    "открой": "открой", "аткрой": "открой", "аткрй": "открой",
    "запусти": "запусти", "запустиь": "запусти", "запст": "запусти",
    "включи": "включи", "вклюи": "включи", "вклчи": "включи",
    "закрой": "закрой", "закрй": "закрой", "закро": "закрой",
    "браузер": "браузер", "брузер": "браузер", "браузр": "браузер",
    "хром": "хром", "хрм": "хром",
    "телеграм": "телеграм", "телеграмм": "телеграм", "тг": "тг",
    "дискорд": "дискорд", "дискорд": "дискорд", "дискордд": "дискорд",
    "спотифай": "спотифай", "спотифайй": "спотифай", "спотивай": "спотифай",
    "майнкрафт": "майнкрафт", "минкрафт": "майнкрафт", "майнкравт": "майнкрафт",
    "калькулятор": "калькулятор", "калькулятр": "калькулятор",
    "блокнот": "блокнот", "блакнот": "блокнот",
    "громче": "громче", "громч": "громче",
    "тише": "тише", "тше": "тише",
    "стим": "стим", "стеам": "стим", "стем": "стим",
    "музыку": "музыку", "музыка": "музыка", "музык": "музыку",
    "песню": "песню", "песнюь": "песню", "песна": "песню",
    "погоду": "погоду", "погод": "погоду", "пагоду": "погоду",
    "время": "время", "врем": "время", "времмя": "время",
    "найди": "найди", "найди": "найди", "найд": "найди",
    "гугли": "гугли", "гугл": "гугли", "загугли": "загугли",
    "поставь": "поставь", "поставьь": "поставь", "постав": "поставь",
    "таймер": "таймер", "таймерь": "таймер", "тамер": "таймер",
    "напомни": "напомни", "напомниь": "напомни", "напомн": "напомни",
    "скинь": "скинь", "скиньь": "скинь",
    "сделай": "сделай", "сделайь": "сделай", "сдела": "сделай",
    "скриншот": "скриншот", "скриншотт": "скриншот", "скриншот.": "скриншот",
    "выключи": "выключи", "выклчи": "выключи", "выключить": "выключи",
    # Новые расширенные коррекции
    "пятницаа": "пятница", "пятнице": "пятница", "патнице": "пятница",
    "бар": "барк", "брак": "барк", "баркк": "барк",
    "патнице": "пятница", "пятнце": "пятница", "пятницау": "пятница",
    "пятницуу": "пятницу", "пятницуа": "пятницу",
    "откройе": "открой", "открыой": "открой", "аткройе": "открой",
    "найдины": "найди", "найдиь": "найди", "найти": "найди",
    "запустиь": "запусти", "запускай": "запусти", "запуст": "запусти",
    "вклюи": "включи", "вклучи": "включи", "включить": "включи",
    "браузар": "браузер", "броузер": "браузер", "брауер": "браузер",
    "телеграмм": "телеграм", "тг": "телеграм", "телеграма": "телеграм",
    "дискордд": "дискорд", "дискор": "дискорд", "дискорд": "дискорд",
    "спотифайй": "спотифай", "спотивай": "спотифай", "спотifaй": "спотифай",
    "майнкравт": "майнкрафт", "майкрафт": "майнкрафт", "мincraft": "майнкрафт",
    "калькуляторр": "калькулятор", "калькулятр": "калькулятор", "калкулятор": "калькулятор",
    "блакнот": "блокнот", "блокнотт": "блокнот", "бланкот": "блокнот",
    "громче": "громче", "громч": "громче", "громчеа": "громче",
    "тише": "тише", "тше": "тише", "тиша": "тише",
    "стеам": "стим", "стем": "стим", "стимм": "стим", "стеэм": "стим",
    "музык": "музыку", "музыкаа": "музыка", "музыкуу": "музыку",
    "песнюь": "песню", "песна": "песню", "песнюю": "песню",
    "пагоду": "погоду", "погодуу": "погоду", "погодка": "погоду",
    "времмя": "время", "врем": "время", "времмяя": "время",
    "гуглиь": "гугли", "гугл": "гугли", "загуглиь": "загугли",
    "поставьь": "поставь", "постав": "поставь", "поставььь": "поставь",
    "тамер": "таймер", "таймерь": "таймер", "таймерр": "таймер",
    "напомниь": "напомни", "напомн": "напомни", "напомнии": "напомни",
    "скиньь": "скинь", "скин": "скинь", "скиинь": "скинь",
    "сделайь": "сделай", "сдела": "сделай", "сделайй": "сделай",
    "скриншотт": "скриншот", "скриншот.": "скриншот", "скришот": "скриншот",
    "перезагрузиь": "перезагрузи", "перезагруз": "перезагрузи",
    "ютубе": "ютуб", "ютюбе": "ютуб", "ютубб": "ютуб", "ютубеe": "ютуб",
    "вк": "вк", "вконта": "вконтакте", "вконтакт": "вконтакте",
    "доту": "доту", "дота": "доту", "дотуу": "доту",
    "контру": "контру", "контра": "контру", "каунтер": "контру",
    "набережные": "набережные", "челны": "челны", "казань": "казань",
    "температура": "температуру", "температуры": "температуру",
    "переведи": "переведи", "перевод": "переведи", "перевиди": "переведи",
    "расскажи": "расскажи", "раскажи": "расскажи", "расскажы": "расскажи",
    "показывай": "покажи", "покажы": "покажи", "покажии": "покажи",
    "напиши": "напиши", "напишиь": "напиши", "напишии": "напиши",
    "ответь": "ответь", "атветь": "ответь", "ответьь": "ответь",
    "спасибо": "спасибо", "спс": "спасибо", "спасиб": "спасибо",
    "повтори": "повтори", "повториь": "повтори", "повтории": "повтори",
    "останови": "останови", "стоп": "стоп", "хватит": "хватит",
    "пауза": "пауза", "пайза": "пауза", "паузу": "пауза",
    "продолжи": "продолжи", "продолжить": "продолжи", "продолжы": "продолжи",
    "выключить": "выключи", "включить": "включи", "открыть": "открой",
    "закрыть": "закрой", "запустить": "запусти", "найти": "найди",
    "поставить": "поставь", "сделать": "сделай", "сказать": "скажи",
    "перезагрузи": "перезагрузи", "перезагрузиь": "перезагрузи",
    # Английские слова которые ASR пишет по-русски
    "вэбсайт": "website", "вебсайт": "website",
    "ютуб": "ютуб", "ютубе": "ютуб", "ютюбе": "ютуб",
}

# Загружаем кастомные коррекции из файла
_ASR_CORRECTIONS_PATH = os.path.join(_APP_DIR, "asr_corrections.json")

def _load_asr_corrections():
    """Загружает кастомные коррекции из asr_corrections.json."""
    global _ASR_CORRECTIONS
    try:
        if os.path.exists(_ASR_CORRECTIONS_PATH):
            with open(_ASR_CORRECTIONS_PATH, "r", encoding="utf-8") as f:
                custom = json.load(f)
            _ASR_CORRECTIONS.update(custom)
            print(f"[ASR] Кастомных коррекций: {len(custom)}")
    except Exception as e:
        print(f"[ASR] Ошибка загрузки коррекций: {e}")

_load_asr_corrections()

def apply_text_corrections(text: str) -> str:
    """Исправляет частые ошибки распознавания."""
    if not text:
        return text

    # 1. Точные замены слов (каждое слово отдельно)
    words = text.split()
    corrected_words = []
    for word in words:
        clean = word.strip(".,!?;:-").lower()
        if clean in _ASR_CORRECTIONS:
            replacement = _ASR_CORRECTIONS[clean]
            # Сохраняем регистр первой буквы
            if word[0].isupper():
                replacement = replacement[0].upper() + replacement[1:]
            corrected_words.append(replacement + word[len(clean):] if len(word) > len(clean) else replacement)
        else:
            corrected_words.append(word)

    result = " ".join(corrected_words)

    # 2. Удаление дублей слов ("пятница пятница открой" -> "пятница открой")
    result = re.sub(r"\b(\w+)\s+\1\b", r"\1", result, flags=re.IGNORECASE)

    # 3. Исправление "пятница" в начале (часто распознаётся как "пятница" без пробела)
    result = re.sub(r"^пятнца\s*", "пятница ", result, flags=re.IGNORECASE)
    result = re.sub(r"^патница\s*", "пятница ", result, flags=re.IGNORECASE)

    # 4. Убираем лишние пробелы
    result = " ".join(result.split())

    return result



# ============================================================
#  VOICE RECOGNIZER (GigaAM v3 E2E RNN-T через onnx-asr)
# ============================================================

class VoiceRecognizer:
    def __init__(self):
        self.model = None
        self.is_ready = False

    def load_model(self, status_callback=None, done_callback=None):
        def _load():
            try:
                if status_callback:
                    status_callback("Загрузка GigaAM v3 E2E RNN-T...")
                if not HAS_ONNX_ASR:
                    if status_callback:
                        status_callback("Ошибка: onnx-asr не установлен")
                    if done_callback:
                        done_callback(False)
                    return
                self.model = onnx_asr.load_model(ASR_MODEL_NAME)
                self.is_ready = True
                self.warmup()
                if status_callback:
                    status_callback("GigaAM v3 E2E RNN-T готов")
                if done_callback:
                    done_callback(True)
            except Exception as e:
                print(f"[GigaAM v3] ОШИБКА: {e}")
                traceback.print_exc()
                if status_callback:
                    status_callback(f"Ошибка: {e}")
                if done_callback:
                    done_callback(False)
        threading.Thread(target=_load, daemon=True).start()

    def transcribe(self, audio_np: np.ndarray) -> str:
        if not self.is_ready or self.model is None:
            return ""
        try:
            audio_processed = preprocess_audio(audio_np, ASR_SAMPLE_RATE)
            tail_pad = np.zeros(int(1.0 * ASR_SAMPLE_RATE), dtype=audio_processed.dtype)
            audio_processed = np.concatenate([audio_processed, tail_pad])
            audio_f32 = audio_processed.astype(np.float32) / 32768.0
            tmp_wav = tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, prefix="friday_asr_"
            )
            tmp_wav.close()
            sf.write(tmp_wav.name, audio_f32, ASR_SAMPLE_RATE)
            result = self.model.recognize(tmp_wav.name)
            if isinstance(result, list):
                text = result[0] if result else ""
            else:
                text = result
            text = text.strip()
            # Двойное распознавание для коротких фраз (< 30 символов)
            # Если результат короткий и содержит мусор — пробуем ещё раз
            if len(text) < 30 and text:
                try:
                    result2 = self.model.recognize(tmp_wav.name)
                    if isinstance(result2, list) and result2:
                        text2 = result2[0].strip()
                    elif isinstance(result2, str):
                        text2 = result2.strip()
                    else:
                        text2 = ""
                    # Если второй результат длиннее и осмысленнее — берём его
                    if len(text2) > len(text) and text2:
                        text = text2
                except Exception:
                    pass
            try:
                os.unlink(tmp_wav.name)
            except Exception:
                pass
            if text:
                text = apply_text_corrections(text)
            return text
        except Exception as e:
            print(f"[GigaAM v3] Ошибка распознавания: {e}")
            traceback.print_exc()
            return ""

    def warmup(self):
        """Прогрев модели — первые 2-3 распознавания медленные."""
        try:
            print("[ASR] Прогрев модели...")
            silence = np.zeros(ASR_SAMPLE_RATE, dtype=np.int16)
            tmp_wav = tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, prefix="friday_warmup_"
            )
            tmp_wav.close()
            audio_f32 = silence.astype(np.float32) / 32768.0
            sf.write(tmp_wav.name, audio_f32, ASR_SAMPLE_RATE)
            self.model.recognize(tmp_wav.name)
            os.unlink(tmp_wav.name)
            print("[ASR] Прогрев завершён")
        except Exception as e:
            print(f"[ASR] Ошибка прогрева: {e}")


# ============================================================
#  FRIDAY BRAIN — ядро
# ============================================================

class FridayBrain:
    def __init__(self):
        self.learner = SelfLearner()
        self.learner.load_custom_programs()
        self.speaker = Speaker()
        self.recognizer = VoiceRecognizer()
        self.is_processing = False
        self.is_listening = True
        self.is_learning_program = False
        self._pending_program_name = None
        self._log_callback = None
        self._status_callback = None
        self._processing_lock = threading.Lock()
        self._mic_level = 0
        self._llm_thread = None
        self._was_speaking = False

    def set_log_callback(self, callback):
        self._log_callback = callback

    def set_status_callback(self, callback):
        self._status_callback = callback

    def log(self, msg: str):
        if self._log_callback:
            try:
                self._log_callback(msg)
            except Exception:
                pass
        print(f"[FRIDAY] {msg}")

    def set_status(self, text: str):
        if self._status_callback:
            try:
                self._status_callback(text)
            except Exception:
                pass
        print(f"[STATUS] {text}")

    def start_model(self):
        # Silero removed
        self.recognizer.load_model(
            status_callback=lambda s: self.set_status(s),
            done_callback=lambda ok: self._on_model_ready(ok)
        )

    def _on_model_ready(self, success: bool):
        if success:
            self.log("GigaAM v3 E2E RNN-T загружена.")
            self.log(f"Микрофон: {MIC_SAMPLE_RATE} Гц -> {ASR_SAMPLE_RATE} Гц")
            if self.speaker.use_bark:
                tts_mode = "Bark"
                tts_voice = self.speaker.bark_speaker
            else:
                tts_mode = "edge_tts"
                tts_voice = self.speaker.voice
            self.log(f"TTS: {tts_mode}, голос: {tts_voice}")
            self.log(f"ASR: {ASR_MODEL_NAME}")
            self.log(f"LLM: {LLM_MODEL}")
            self.log("Веб-поиск: Wikipedia + DuckDuckGo + стихи")
            self.log("Стоп: прерывает думание и говорение")
            self.set_status("СЛУШАЮ")
            threading.Thread(target=self._listen_loop, daemon=True).start()
        else:
            self.set_status("Ошибка загрузки ASR")
            self.log("Не удалось загрузить GigaAM v3!")

    def test_mic(self):
        self.log(f"Тест микрофона ({MIC_SAMPLE_RATE} Гц)...")
        threading.Thread(target=self._test_mic_thread, daemon=True).start()

    def _test_mic_thread(self):
        try:
            CHUNK = 8000
            stream = sd.InputStream(samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK)
            stream.start()
            time.sleep(0.1)
            data, _ = stream.read(CHUNK)
            stream.stop()
            stream.close()
            audio_data = np.array(data, dtype=np.int16).flatten()
            max_val = np.max(np.abs(audio_data))
            rms = np.sqrt(np.mean(audio_data.astype(np.float32) ** 2))
            resampled = resample_48k_to_16k(audio_data)
            max_res = np.max(np.abs(resampled))
            rms_res = np.sqrt(np.mean(resampled.astype(np.float32) ** 2))
            self.log(f"Уровень: max={max_val}, rms={rms:.1f} ({MIC_SAMPLE_RATE} Гц)")
            self.log(f"После ресэмплинга: max={max_res}, rms={rms_res:.1f} ({ASR_SAMPLE_RATE} Гц)")
            if max_val < 150:
                self.log("Микрофон не слышит")
            elif max_val < 1000:
                self.log("Тихий звук")
            else:
                self.log("Микрофон работает!")
        except Exception as e:
            self.log(f"Ошибка микрофона: {e}")

    def _listen_loop(self):
        CHUNK_MIC = int(0.1 * MIC_SAMPLE_RATE)
        try:
            stream = sd.InputStream(samplerate=MIC_SAMPLE_RATE, channels=1, dtype="int16", blocksize=CHUNK_MIC)
            stream.start()
        except Exception as e:
            self.log(f"Ошибка микрофона: {e}")
            return

        dynamic_threshold = SILENCE_THRESHOLD
        noise_samples = []
        calibration_frames = 0
        CALIBRATION_TARGET = 25  # 2.5 сек калибровки
        recalc_interval = 150  # ещё чаще пересчёт порога
        _noise_update_counter = 0
        cycle_count = 0

        while self.is_listening:
            if self.speaker.is_speaking:
                self._was_speaking = True

            # ============================================================
            #  РЕЖИМ ОБРАБОТКИ — слушаем «стоп»
            # ============================================================
            if self.is_processing:
                if self.speaker.is_speaking:
                    try:
                        stream.read(CHUNK_MIC)
                    except Exception:
                        pass
                    time.sleep(0.05)
                    continue

                try:
                    data, _ = stream.read(CHUNK_MIC)
                except Exception:
                    time.sleep(0.05)
                    continue
                audio_data = np.array(data, dtype=np.int16).flatten()
                audio_16k = resample_48k_to_16k(audio_data)
                rms = np.sqrt(np.mean(audio_16k.astype(np.float32) ** 2))
                self._mic_level = min(100, int(rms / 50))

                if rms < dynamic_threshold:
                    continue

                audio_chunks_16k = [audio_16k.copy()]
                silence_timer = 0.0
                phrase_duration = 0.1

                while self.is_listening and self.is_processing and not self.speaker.is_speaking:
                    try:
                        data2, _ = stream.read(CHUNK_MIC)
                    except Exception:
                        continue
                    chunk_data = np.array(data2, dtype=np.int16).flatten()
                    chunk_16k = resample_48k_to_16k(chunk_data)
                    audio_chunks_16k.append(chunk_16k.copy())
                    rms2 = np.sqrt(np.mean(chunk_16k.astype(np.float32) ** 2))
                    self._mic_level = min(100, int(rms2 / 50))
                    if rms2 < dynamic_threshold * 0.35:
                        silence_timer += 0.1
                    else:
                        silence_timer = 0.0
                    phrase_duration += 0.1
                    if silence_timer >= SILENCE_WAIT:
                        break
                    if phrase_duration >= MAX_PHRASE_DURATION:
                        break

                if not self.is_processing:
                    continue

                full_audio_16k = np.concatenate(audio_chunks_16k)

                if silence_timer > 0:
                    trim = int(silence_timer * ASR_SAMPLE_RATE)
                    keep_tail = int(0.5 * ASR_SAMPLE_RATE)
                    trim = max(0, trim - keep_tail)
                    if trim > 0 and trim < len(full_audio_16k):
                        full_audio_16k = full_audio_16k[:-trim]

                if len(full_audio_16k) < ASR_SAMPLE_RATE * 0.3:
                    continue

                self.log("Слушаю во время обработки...")
                text = self.recognizer.transcribe(full_audio_16k)
                if text:
                    text_lower = text.lower().strip()
                    self.log(f"Во время обработки услышал: {text}")
                    for trig in STOP_TRIGGERS:
                        if trig in text_lower:
                            self._interrupt()
                            break
                continue

            # ============================================================
            # ОБЫЧНЫЙ РЕЖИМ
            # ============================================================

            if self._was_speaking:
                self._was_speaking = False
                time.sleep(0.3)
                continue

            if calibration_frames < CALIBRATION_TARGET:
                try:
                    data, _ = stream.read(CHUNK_MIC)
                    audio_data = np.array(data, dtype=np.int16).flatten()
                    audio_16k = resample_48k_to_16k(audio_data)
                    rms = np.sqrt(np.mean(audio_16k.astype(np.float32) ** 2))
                    self._mic_level = min(100, int(rms / 50))
                    noise_samples.append(rms)
                    calibration_frames += 1
                    if calibration_frames == CALIBRATION_TARGET:
                        median_noise = np.median(noise_samples)
                        dynamic_threshold = max(SILENCE_THRESHOLD, int(median_noise * 2))
                        self.log(f"Калибровка: порог={dynamic_threshold}")
                    continue
                except Exception:
                    continue

            cycle_count += 1
            if cycle_count % recalc_interval == 0:
                if noise_samples:
                    recent = noise_samples[-10:] if len(noise_samples) >= 10 else noise_samples
                    median_noise = np.median(recent)
                    new_threshold = max(SILENCE_THRESHOLD, int(median_noise * 2))
                    dynamic_threshold = int(dynamic_threshold * 0.5 + new_threshold * 0.5)

            speech_detected = False
            consecutive_loud = 0
            last_audio_16k = None
            preroll_buffer = []
            PREROLL_SIZE = 6  # 6 чанков = 0.6с до речи — ловит самое начало

            while self.is_listening and not self.speaker.is_speaking and not self.is_processing:
                try:
                    data, _ = stream.read(CHUNK_MIC)
                except Exception:
                    continue
                audio_data = np.array(data, dtype=np.int16).flatten()
                audio_16k = resample_48k_to_16k(audio_data)
                rms = np.sqrt(np.mean(audio_16k.astype(np.float32) ** 2))
                self._mic_level = min(100, int(rms / 50))
                preroll_buffer.append(audio_16k.copy())
                if len(preroll_buffer) > PREROLL_SIZE:
                    preroll_buffer.pop(0)
                if rms >= dynamic_threshold:
                    consecutive_loud += 1
                    if consecutive_loud >= 1:
                        speech_detected = True
                        last_audio_16k = np.concatenate(preroll_buffer) if preroll_buffer else audio_16k.copy()
                        break
                else:
                    consecutive_loud = 0

            if not speech_detected or not self.is_listening:
                continue

            audio_chunks_16k = []
            silence_timer = 0.0
            phrase_duration = 0.0
            if last_audio_16k is not None:
                audio_chunks_16k.append(last_audio_16k)
                phrase_duration += len(last_audio_16k) / ASR_SAMPLE_RATE

            chunk_time_16k = 0.1

            while self.is_listening and not self.speaker.is_speaking and not self.is_processing:
                try:
                    data, _ = stream.read(CHUNK_MIC)
                except Exception:
                    continue
                audio_data = np.array(data, dtype=np.int16).flatten()
                chunk_16k = resample_48k_to_16k(audio_data)
                audio_chunks_16k.append(chunk_16k.copy())
                rms = np.sqrt(np.mean(chunk_16k.astype(np.float32) ** 2))
                self._mic_level = min(100, int(rms / 50))
                if rms < dynamic_threshold * 0.35:
                    silence_timer += chunk_time_16k
                else:
                    silence_timer = 0.0
                phrase_duration += chunk_time_16k
                if silence_timer >= SILENCE_WAIT:
                    break
                if phrase_duration >= MAX_PHRASE_DURATION:
                    break

            if not self.is_listening:
                break
            if not audio_chunks_16k:
                continue

            full_audio_16k = np.concatenate(audio_chunks_16k)

            if silence_timer > 0:
                trim_samples = int(silence_timer * ASR_SAMPLE_RATE)
                if trim_samples > 0 and trim_samples < len(full_audio_16k):
                    keep_tail = int(0.5 * ASR_SAMPLE_RATE)
                    trim_samples = max(0, trim_samples - keep_tail)
                    full_audio_16k = full_audio_16k[:-trim_samples] if trim_samples > 0 else full_audio_16k

            if len(full_audio_16k) < ASR_SAMPLE_RATE * 0.3:
                continue

            signal_rms = np.sqrt(np.mean(full_audio_16k.astype(np.float32) ** 2))
            if signal_rms < dynamic_threshold * 0.25:
                continue

            self.log(f"Распознавание... (уровень: {signal_rms:.0f})")
            text = self.recognizer.transcribe(full_audio_16k)

            if text:
                text = text.strip()
                self._on_text(text)

        try:
            stream.stop()
            stream.close()
        except Exception:
            pass

    def _begin_processing(self) -> bool:
        with self._processing_lock:
            if self.is_processing:
                return False
            self.is_processing = True
            return True

    def _done(self):
        with self._processing_lock:
            self.is_processing = False

    def _on_text(self, text: str):
        text_lower = text.lower().strip()
        for trig in STOP_TRIGGERS:
            if trig in text_lower:
                self._interrupt()
                return

        if not self._begin_processing():
            return
        found_friday, command_text = detect_friday(text)
        if not found_friday:
            self._done()
            return
        self.log(f"Вы сказали: {text}")
        command_text = command_text.strip()
        if not command_text:
            self.speaker.say("Да, босс?")
            self._done()
            return
        self._dispatch_command(command_text)

    def _interrupt(self):
        self.log("Получен СТОП — прерываю")
        _stop_event.set()
        self.speaker.stop_speaking()
        self.set_status("СЛУШАЮ")
        self._done()

    def send_text_command(self, text: str) -> str:
        def _run():
            if not self._begin_processing():
                self.log("Уже обрабатываю, подождите...")
                return
            self.log(f"Текстовая команда: {text}")
            self._dispatch_command(text)
        threading.Thread(target=_run, daemon=True).start()
        return "Команда принята, босс."

    def _dispatch_command(self, command_text: str):
        global DIALOG_HISTORY
        command_text = command_text.strip()
        if not command_text:
            self.speaker.say("Да, босс?")
            self._done()
            return

        for trig in STOP_TRIGGERS:
            if trig in command_text.lower():
                self._interrupt()
                return

        _repeat_triggers = ["повтори", "что ты сказала", "не расслышал", "не понял", "повтори снова", "скажи ещё раз"]
        if command_text.lower().strip() in _repeat_triggers:
            if DIALOG_HISTORY and DIALOG_HISTORY[-1].get("role") == "assistant":
                self.speaker.say(DIALOG_HISTORY[-1]["content"])
            else:
                self.speaker.say("Нечего повторять, босс.")
            self._done()
            return

        if self.is_learning_program and self._pending_program_name:
            self._handle_program_path(command_text)
            return

        for trig in LEARN_TRIGGERS:
            if trig in command_text.lower():
                self._start_program_learning(command_text)
                return

        cancel_result = try_cancel_shutdown(command_text)
        if cancel_result:
            self.speaker.say(cancel_result)
            self._done()
            return

        if is_model_close_command(command_text):
            self.log("MODEL:close")
            self.speaker.say("Убираю модель, босс.")
            self._done()
            return

        if is_model_command(command_text):
            self.log("MODEL:open")
            self.speaker.say("Открываю модель костюма Железного человека, босс.")
            self._done()
            return

        if is_satellite_close_command(command_text):
            self.log("SATELLITE:close")
            self.speaker.say("Убираю карту, босс.")
            self._done()
            return

        if is_panorama_command(command_text):
            city = extract_city(command_text)
            self.log(f"SATELLITE:panorama:{city}")
            if city:
                self.speaker.say(f"Открываю панораму улиц {city}, босс.")
            else:
                self.speaker.say("Открываю панораму улиц, босс.")
            self._done()
            return

        if is_satellite_command(command_text):
            city = extract_city(command_text)
            self.log(f"SATELLITE:open:{city}")
            if city:
                self.speaker.say(f"Открываю спутниковую карту {city}, босс.")
            else:
                self.speaker.say("Открываю спутниковую карту, босс.")
            self._done()
            return

        if is_time_command(command_text):
            response = get_time_response()
            self.log(f"Время: {response}")
            self.speaker.say(response)
            DIALOG_HISTORY.append({"role": "user", "content": command_text})
            DIALOG_HISTORY.append({"role": "assistant", "content": response})
            self._done()
            return

        if is_weather_command(command_text):
            response = get_weather(command_text, self.speaker.say)
            self.log(f"Погода: {response}")
            self.speaker.say(response)
            DIALOG_HISTORY.append({"role": "user", "content": command_text})
            DIALOG_HISTORY.append({"role": "assistant", "content": response})
            self._done()
            return

        if is_joke_command(command_text):
            response = get_joke()
            self.log(f"Шутка: {response}")
            self.speaker.say(response)
            self._done()
            return

        if is_timer_command(command_text):
            response = set_timer(command_text, self.speaker.say)
            self.log(f"Таймер: {response}")
            self.speaker.say(response)
            self._done()
            return

        if is_screenshot_command(command_text):
            response = take_screenshot()
            self.log(f"Скриншот: {response}")
            self.speaker.say(response)
            self._done()
            return

        if is_news_command(command_text):
            response = get_news()
            self.log(f"Новости: {response}")
            self.speaker.say(response)
            self._done()
            return

        if is_battery_command(command_text):
            response = get_battery_status()
            self.log(f"Батарея: {response}")
            self.speaker.say(response)
            self._done()
            return

        if is_translate_command(command_text):
            response = do_translate(command_text)
            self.log(f"Перевод: {response}")
            self.speaker.say(response)
            self._done()
            return

        sys_action = is_system_command(command_text)
        if sys_action:
            response = execute_system_command(sys_action)
            self.log(f"Система: {response}")
            self.speaker.say(response)
            self._done()
            return

        

        result = try_close_command(command_text)
        if result:
            self.speaker.say(result)
            self._done()
            return

        result = try_music_command(command_text)
        if result:
            self.speaker.say(result)
            self._done()
            return

        

        result = try_open_program(command_text)
        if result:
            self.speaker.say(result)
            self._done()
            return

        # Команды переключения голоса
        ct = command_text.lower().strip()

        if any(w in ct for w in ["включи барк", "голос барк", "эмоции вкл", "эмоциональный голос"]):
            if self.speaker.load_bark():
                self.speaker.say("Эмоциональный режим активирован, босс! [laughs] Теперь я буду говорить с чувствами!")
            else:
                self.speaker.say("Bark не установлен, босс. Установите через pip install.")
            self._done()
            return

        if any(w in ct for w in ["обычный голос", "выключи барк", "верни голос", "эмоции выкл"]):
            self.speaker.use_bark = False
            self.speaker.say("Вернула обычный голос, босс.")
            self._done()
            return

        if any(w in ct for w in ["смени голос", "поменяй голос", "какой голос"]):
            voices = ["v2/ru_speaker_0", "v2/ru_speaker_1", "v2/ru_speaker_2",
                       "v2/ru_speaker_3", "v2/ru_speaker_4", "v2/ru_speaker_5",
                       "v2/ru_speaker_6", "v2/ru_speaker_7", "v2/ru_speaker_8", "v2/ru_speaker_9"]
            if "0" in ct: self.speaker.bark_speaker = voices[0]
            elif "1" in ct: self.speaker.bark_speaker = voices[1]
            elif "2" in ct: self.speaker.bark_speaker = voices[2]
            elif "3" in ct: self.speaker.bark_speaker = voices[3]
            elif "4" in ct: self.speaker.bark_speaker = voices[4]
            elif "5" in ct: self.speaker.bark_speaker = voices[5]
            elif "6" in ct: self.speaker.bark_speaker = voices[6]
            elif "7" in ct: self.speaker.bark_speaker = voices[7]
            elif "8" in ct: self.speaker.bark_speaker = voices[8]
            elif "9" in ct: self.speaker.bark_speaker = voices[9]
            else: self.speaker.bark_speaker = random.choice(voices)
            self.speaker.say(f"Голос изменён на {self.speaker.bark_speaker}, босс.")
            self._done()
            return

        # Команда добавления коррекции ASR
        if any(w in ct for w in ["добавь коррекцию", "исправь распознавание", "запомни слово"]):
            # Формат: "Пятница, добавь коррекцию слово->правильно"
            match = re.search(r"коррекцию\s+(.+?)\s*->\s*(.+)", command_text)
            if match:
                wrong = match.group(1).strip().lower()
                right = match.group(2).strip().lower()
                _ASR_CORRECTIONS[wrong] = right
                try:
                    custom = {}
                    if os.path.exists(_ASR_CORRECTIONS_PATH):
                        with open(_ASR_CORRECTIONS_PATH, "r", encoding="utf-8") as f:
                            custom = json.load(f)
                    custom[wrong] = right
                    with open(_ASR_CORRECTIONS_PATH, "w", encoding="utf-8") as f:
                        json.dump(custom, f, ensure_ascii=False, indent=2)
                    self.speaker.say(f"Запомнила: {wrong} будет {right}, босс.")
                except Exception:
                    self.speaker.say("Не удалось сохранить, босс.")
            else:
                self.speaker.say("Скажи: добавь коррекцию слово стрелка правильно, босс.")
            self._done()
            return

        result = try_search_command(command_text)
        if result:
            self.speaker.say(result)
            self._done()
            return

        self._process_with_ai(command_text)

    def _process_with_ai(self, user_text: str):
        global DIALOG_HISTORY
        self.log("Думаю...")
        self.set_status("ДУМАЮ")
        _stop_event.clear()

        web_context = ""
        use_web = needs_web_search(user_text)
        if use_web:
            self.log(f"[WEB] Поиск: {user_text}")
            web_context = get_web_context(user_text)
            if web_context:
                self.log(f"[WEB] Найден контекст ({len(web_context)} символов)")
            else:
                self.log("[WEB] Ничего не найдено, отвечаю из знаний LLM")

        if web_context:
            full_prompt = (
                f"Вопрос пользователя: {user_text}\n\n"
                f"Информация из интернета:\n{web_context}\n\n"
                f"Ответь на вопрос, используя эту информацию. "
                f"Если это стих — приведи его текст ТОЧНО как в источнике, слово в слово. "
                f"НИКОГДА не придумывай стихи самостоятельно. "
                f"Если в источнике нет текста стиха — скажи: не нашла стих, босс."
            )
            messages = [{"role": "system", "content": SYSTEM_PROMPT_WITH_WEB}]
            messages.extend(DIALOG_HISTORY[-DIALOG_HISTORY_MAX:])
            messages.append({"role": "user", "content": full_prompt})
        else:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages.extend(DIALOG_HISTORY[-DIALOG_HISTORY_MAX:])
            messages.append({"role": "user", "content": user_text})

        self.log("[LLM] Отправляю prompt")
        if web_context:
            self.log("[LLM] Контекст из интернета вшит в prompt")

        result_container = {"resp": None, "error": None}

        def _chat():
            try:
                # 1. Сначала облако (SiliconFlow)
                if _cloud_client.api_key:
                    try:
                        cloud_resp = _cloud_client.chat.completions.create(
                            model=LLM_CLOUD_MODEL,
                            messages=messages,
                            max_tokens=2048,
                            temperature=0.7,
                            timeout=30,
                        )
                        result_container["resp"] = cloud_resp.choices[0].message.content.strip()
                        return
                    except Exception as cloud_err:
                        self.log(f"[LLM] Облако недоступно: {cloud_err}")
                # 2. Fallback на локальную Ollama
                if HAS_OLLAMA:
                    result_container["resp"] = ollama.chat(model=LLM_MODEL, messages=messages)
                else:
                    result_container["error"] = "Ollama не установлена"
            except Exception as e:
                result_container["error"] = str(e)
        

        t = threading.Thread(target=_chat, daemon=True)
        self._llm_thread = t
        t.start()

        elapsed = 0
        timeout = 35
        while elapsed < timeout:
            if _stop_event.is_set():
                self.log("LLM прерван командой СТОП")
                self.set_status("СЛУШАЮ")
                self._done()
                return
            if not t.is_alive():
                break
            t.join(timeout=0.1)
            elapsed += 0.1

        if t.is_alive():
            self.log(f"Таймаут LLM ({timeout}с)")
            if web_context:
                self.speaker.say(web_context)
            else:
                self.speaker.say("Думаю слишком долго, босс.")
            DIALOG_HISTORY.append({"role": "user", "content": user_text})
            DIALOG_HISTORY.append({"role": "assistant", "content": web_context if web_context else "Думаю слишком долго, босс."})
            self.set_status("СЛУШАЮ")
            self._done()
            return

        if result_container["error"]:
            self.log(f"Ошибка LLM: {result_container['error']}")
            if web_context:
                self.speaker.say(web_context)
            else:
                self.speaker.say("Ошибка, босс.")
            DIALOG_HISTORY.append({"role": "user", "content": user_text})
            DIALOG_HISTORY.append({"role": "assistant", "content": web_context if web_context else "Ошибка, босс."})
            self.set_status("СЛУШАЮ")
            self._done()
            return

        if result_container["resp"] is None:
            self.speaker.say("Не поняла, босс.")
            DIALOG_HISTORY.append({"role": "user", "content": user_text})
            DIALOG_HISTORY.append({"role": "assistant", "content": "Не поняла, босс."})
            self.set_status("СЛУШАЮ")
            self._done()
            return

        try:
            raw = result_container["resp"]
            # Облако возвращает строку, Ollama — объект с .message.content
            if isinstance(raw, str):
                content = raw
            else:
                content = raw.get("message", {}).get("content", "") if isinstance(raw, dict) else raw.message.content
            if not content or not content.strip():
                content = "Не поняла команду, босс."
            content = re.sub(r'<think\s*>.*?</think\s*>', '', content, flags=re.DOTALL).strip()
            if not content:
                content = "Не поняла команду, босс."
            self.log(f"Пятница: {content}")
            
            self.speaker.say(content)
            DIALOG_HISTORY.append({"role": "user", "content": user_text})
            DIALOG_HISTORY.append({"role": "assistant", "content": content})
            if len(DIALOG_HISTORY) > DIALOG_HISTORY_MAX * 2:
                DIALOG_HISTORY = DIALOG_HISTORY[-DIALOG_HISTORY_MAX * 2:]
        except Exception as e:
            self.log(f"Ошибка обработки ответа: {e}")
            traceback.print_exc()
            self.speaker.say("Ошибка, босс.")
        
        finally:
            self.set_status("СЛУШАЮ")
            self._done()

    def _start_program_learning(self, text: str):

        text_lower = text.lower()
        for trig in LEARN_TRIGGERS:
            if trig in text_lower:
                name = text_lower.replace(trig, "").strip()
                if name:
                    self._pending_program_name = name
                    break
        if self._pending_program_name:
            self.speaker.say(f"Босс, назовите путь к программе {self._pending_program_name}.")
            self.is_learning_program = True
        else:
            self.speaker.say("Босс, как называется программа?")
            self.is_learning_program = True
            self._pending_program_name = None
        self._done()

    def _handle_program_path(self, text: str):
        path = text.strip()
        if not self._pending_program_name:
            self._pending_program_name = path
            self.speaker.say(f"Босс, назовите путь к программе {path}.")
            self._done()
            return
        name = self._pending_program_name
        if ":" in path and "\\" in path:
            self.learner.learn_program(name, path)
            self.log(f"ОБУЧЕНИЕ: «{name}» сохранена: {path}")
            self.speaker.say(f"Запомнила, босс. Программа {name} добавлена.")
        else:
            self.speaker.say("Босс, это не похоже на путь.")
        self.is_learning_program = False
        self._pending_program_name = None
        self._done()

    def stop(self):
        self.is_listening = False
        _stop_music()
        if HAS_KEYBOARD:
            keyboard.unhook_all()

    def get_system_info(self) -> dict:
        try:
            import psutil
            return {
                "cpu": psutil.cpu_percent(),
                "memory": psutil.virtual_memory().percent,
                "memory_total": round(psutil.virtual_memory().total / (1024**3), 1),
                "uptime": str(timedelta(seconds=int(time.time() - psutil.boot_time()))),
                "processes": len(psutil.pids()),
            }
        except ImportError:
            return {"cpu": 0, "memory": 0, "memory_total": 0, "uptime": "?", "processes": 0}

    def get_dialog_history(self) -> list:
        return DIALOG_HISTORY[-20:]

    def get_model_status(self) -> dict:
        return {
            "ready": self.recognizer.is_ready,
            "asr_model": ASR_MODEL_NAME,
            "llm_model": LLM_MODEL,
            "tts_mode": "bark" if self.speaker.use_bark else "edge_tts",
            "tts_speaker": self.speaker.bark_speaker if self.speaker.use_bark else self.speaker.voice,
            "programs_count": len(PROGRAMS),
        }


# ============================================================
#  ТОЧКА ВХОДА
# ============================================================


# ============================================================
#  PYWEBVIEW API — мост между фронтендом и FridayBrain
# ============================================================

class PyWebViewApi:
    def __init__(self, brain):
        self.brain = brain
        self._log_entries = []
        self._log_id_counter = 0
        self._original_log = brain.log
        brain.log = self._hook_log
        self._muted = False

    def _hook_log(self, msg):
        self._log_id_counter += 1
        self._log_entries.append({"id": self._log_id_counter, "msg": msg})
        if len(self._log_entries) > 200:
            self._log_entries = self._log_entries[-200:]
        self._original_log(msg)

    def get_logs(self, last_id=0):
        return [e for e in self._log_entries if e["id"] > last_id]

    def get_status(self):
        return {
            "processing": getattr(self.brain, "is_processing", False),
            "listening": getattr(self.brain, "is_listening", False),
        }

    def get_mic_level(self):
        return getattr(self.brain, "_mic_level", 0)

    def get_system_info(self):
        try:
            return self.brain.get_system_info()
        except Exception:
            return {"cpu": 0, "memory": 0}

    def send_text(self, text):
        threading.Thread(
            target=self.brain._process_command,
            args=(text,),
            daemon=True
        ).start()

    def toggle_mic(self):
        self.brain.is_listening = not self.brain.is_listening
        if self.brain.is_listening:
            threading.Thread(
                target=self.brain._listen_loop,
                daemon=True
            ).start()
        return self.brain.is_listening

    def toggle_mute(self):
        self._muted = not self._muted
        return self._muted

    def minimize_window(self):
        import webview
        webview.windows[0].minimize()

    def toggle_fullscreen(self):
        import webview
        w = webview.windows[0]
        w.toggle_fullscreen()

    def close_window(self):
        self.brain.stop()
        import webview
        webview.windows[0].destroy()



# ============================================================
#  СИСТЕМА ЛИЦЕНЗИРОВАНИЯ
# ============================================================

SUPABASE_URL = "https://pvfzksylzxpxpktymgem.supabase.co"
SUPABASE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InB2Znprc3lsenhweHBrdHltZ2VtIiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODk1ODk0NjMsImV4cCI6MjEwNTE2NTQ2M30.atnj653ishrGqKhtKDXQ0jiDFmTxFXTRFTDayT06yBs"
SECRET_SALT = "wawswdwdwswa"

def _get_license_path():
    """Путь к файлу лицензии в AppData (скрыт от пользователя)."""
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    license_dir = os.path.join(appdata, "JarvisOS")
    os.makedirs(license_dir, exist_ok=True)
    return os.path.join(license_dir, ".license")

def _hash_key(key):
    """Хэширует ключ с солью — так же, как в generate_keys_supabase.py."""
    return hashlib.sha256((key + SECRET_SALT).encode()).hexdigest()

def get_hwid():
    """Уникальный отпечаток железа (5 компонентов)."""
    components = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography")
        guid, _ = winreg.QueryValueEx(key, "MachineGuid")
        winreg.CloseKey(key)
        components.append(guid)
    except Exception:
        components.append("no-guid")
    try:
        r = subprocess.run(["wmic", "cpu", "get", "ProcessorId"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if len(lines) > 1: components.append(lines[-1])
    except Exception:
        components.append("no-cpu")
    try:
        r = subprocess.run(["wmic", "baseboard", "get", "SerialNumber"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if len(lines) > 1: components.append(lines[-1])
    except Exception:
        components.append("no-mb")
    try:
        r = subprocess.run(["wmic", "diskdrive", "get", "SerialNumber"], capture_output=True, text=True, timeout=10)
        lines = [l.strip() for l in r.stdout.strip().split('\n') if l.strip()]
        if len(lines) > 1: components.append(lines[-1])
    except Exception:
        components.append("no-disk")
    import uuid as _uuid
    components.append(str(_uuid.getnode()))
    raw = "|".join(components)
    return hashlib.sha256(raw.encode()).hexdigest()

def is_activated():
    """Проверяет, активирована ли программа (локально)."""
    license_path = _get_license_path()
    if not os.path.exists(license_path):
        return False
    try:
        with open(license_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("hwid") == get_hwid()
    except Exception:
        return False

def activate_key(key):
    """Активирует ключ через Supabase. Возвращает dict с результатом."""
    hwid = get_hwid()
    key_hash = _hash_key(key.upper().strip())
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/license_keys",
            params={
                "key_hash": f"eq.{key_hash}",
                "select": "id,status,hwid"
            },
            headers=headers,
            timeout=15
        )
        if r.status_code != 200:
            return {"ok": False, "error": "Ошибка связи с сервером."}
        rows = r.json()
        if not rows:
            return {"ok": False, "error": "Неверный ключ."}
        row = rows[0]
        if row["status"] == "activated":
            if row.get("hwid") == hwid:
                license_path = _get_license_path()
                with open(license_path, "w", encoding="utf-8") as f:
                    json.dump({"key": key.upper().strip(), "hwid": hwid}, f)
                try:
                    ctypes.windll.kernel32.SetFileAttributesW(license_path, 0x2)
                except Exception:
                    pass
                return {"ok": True, "message": "Активация успешна! Загрузка..."}
            else:
                return {"ok": False, "error": "Ключ уже привязан к другому ПК."}
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/license_keys",
            params={"key_hash": f"eq.{key_hash}"},
            json={
                "status": "activated",
                "hwid": hwid,
                "activated_at": datetime.now().isoformat()
            },
            headers={**headers, "Prefer": "return=minimal"},
            timeout=15
        )
        if r.status_code in (200, 204):
            license_path = _get_license_path()
            with open(license_path, "w", encoding="utf-8") as f:
                json.dump({"key": key.upper().strip(), "hwid": hwid}, f)
            try:
                ctypes.windll.kernel32.SetFileAttributesW(license_path, 0x2)
            except Exception:
                pass
            return {"ok": True, "message": "Активация успешна! Загрузка..."}
        else:
            return {"ok": False, "error": "Не удалось активировать ключ."}
    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": "Нет связи с сервером. Проверьте интернет."}
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


def main():
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


