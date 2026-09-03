"""
Модуль защиты лицензии — stable composite HWID + backward-compat + offline cache.

ИЗМЕНЕНИЯ vs предыдущей версии:
  - get_stable_hwid(): на Windows — composite из MachineGuid + CPU + Disk serial.
    Стабильный между перезагрузками, не плавает на Wi-Fi MAC randomization, выживает
    переустановку драйверов.
  - get_legacy_hwid(): старый MAC-based алгоритм. Сохранён для backward-compat whitelist.
  - check_license(): проверяет сначала stable, потом legacy. На legacy — warning о миграции.
  - offline cache: %LOCALAPPDATA%\\VideoStudio\\license.dat (Windows) / ~/Library/...
    HMAC-SHA256 sig, TTL 7 дней. При network error и валидном cache — пускаем.
"""
import os
import sys
import json
import time
import uuid
import hmac
import hashlib
import platform
import subprocess
import tkinter as tk
import tkinter.messagebox as messagebox
import requests

try:
    import pyperclip
    HAS_PYPERCLIP = True
except ImportError:
    HAS_PYPERCLIP = False

# URL к файлу со списком разрешенных HWID
LICENSE_URL = "https://example.invalid/license/keys.txt"  # sanitized handoff value

# HMAC secret для подписи offline cache. Атакующий может прочитать exe, поэтому это не
# криптостойкость, а защита от тупого копирования license.dat между машинами без модификации.
_LICENSE_SECRET = b"REDACTED_LICENSE_CACHE_HMAC_KEY"  # sanitized handoff value
_CACHE_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Для wmic / других внешних процессов на Windows — скрытие cmd-окна
_SUBPROCESS_HIDE = {}
if sys.platform == "win32":
    _SUBPROCESS_HIDE = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)}


# ─── HWID GENERATION ──────────────────────────────────────────────────

def get_legacy_hwid():
    """
    Старый алгоритм — MD5(MAC). Сохранён для backward-compat whitelist:
    если у юзера старый HWID уже в gist'е, продолжаем пропускать.
    """
    mac = uuid.getnode()
    return hashlib.md5(str(mac).encode()).hexdigest()[:12].upper()


def _read_windows_machine_guid():
    """Reads HKLM\\SOFTWARE\\Microsoft\\Cryptography\\MachineGuid (Windows only)."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        )
        try:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return value
        finally:
            winreg.CloseKey(key)
    except Exception:
        return None


def _get_cpu_brand():
    """CPU brand string (на Windows возвращает модель)."""
    try:
        return platform.processor() or platform.machine() or ""
    except Exception:
        return ""


def _get_disk_serial():
    """Serial физического диска 0. Windows: wmic."""
    if sys.platform != "win32":
        return None
    try:
        result = subprocess.run(
            ["wmic", "diskdrive", "where", "index=0", "get", "SerialNumber"],
            capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=10, **_SUBPROCESS_HIDE
        )
        if result.returncode == 0:
            lines = [l.strip() for l in result.stdout.splitlines() if l.strip()]
            # Output формат: "SerialNumber\n<value>"
            if len(lines) >= 2:
                return lines[1]
    except Exception:
        pass
    return None


def get_stable_hwid():
    """
    Stable composite HWID:
      Windows: MachineGuid + CPU brand + Disk serial → MD5 → 12 chars upper.
      Прочие OS / <2 источников: fallback на legacy MAC-based.

    Свойства:
      - стабилен между перезагрузками
      - не плавает на Wi-Fi MAC randomization
      - выживает переустановку драйверов
      - меняется только при reinstall Windows или замене жёсткого диска
    """
    parts = []

    mg = _read_windows_machine_guid()
    if mg:
        parts.append(f"MG:{mg}")

    cpu = _get_cpu_brand()
    if cpu:
        parts.append(f"CPU:{cpu}")

    disk = _get_disk_serial()
    if disk:
        parts.append(f"DISK:{disk}")

    if len(parts) >= 2:
        combined = "|".join(parts)
        return hashlib.md5(combined.encode()).hexdigest()[:12].upper()

    # Fallback на старый алгоритм если меньше 2 источников (например, на Mac/Linux)
    return get_legacy_hwid()


def get_hwid():
    """
    Public entry. Возвращает stable HWID (с graceful fallback на legacy MAC).
    Старые места кода которые звали get_hwid() — продолжают работать.
    """
    return get_stable_hwid()


# ─── OFFLINE CACHE ──────────────────────────────────────────────────

def _cache_dir():
    """Per-OS cache dir для license.dat."""
    if sys.platform == "win32":
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~/AppData/Local')
    elif sys.platform == "darwin":
        base = os.path.expanduser('~/Library/Application Support')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share')
    p = os.path.join(base, 'VideoStudio')
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass
    return p


def _cache_path():
    return os.path.join(_cache_dir(), 'license.dat')


def _sign(hwid: str, ts: int) -> str:
    msg = f"{hwid}|{ts}".encode()
    return hmac.new(_LICENSE_SECRET, msg, hashlib.sha256).hexdigest()


def _read_cache():
    """Returns (hwid, ts) если cache валиден (signature OK), иначе (None, None)."""
    try:
        with open(_cache_path(), 'r', encoding='utf-8', errors='replace') as f:
            data = json.load(f)
        h = data.get('hwid', '')
        ts = int(data.get('verified_ts', 0))
        sig = data.get('signature', '')
        if h and ts and sig == _sign(h, ts):
            return h, ts
    except Exception:
        pass
    return None, None


def _write_cache(hwid: str):
    """Atomic write license.dat с HMAC signature."""
    try:
        ts = int(time.time())
        data = {'hwid': hwid, 'verified_ts': ts, 'signature': _sign(hwid, ts)}
        tmp = _cache_path() + '.tmp'
        with open(tmp, 'w', encoding='utf-8', errors='replace') as f:
            json.dump(data, f)
        os.replace(tmp, _cache_path())
    except Exception:
        pass


# ─── MAIN LICENSE CHECK ──────────────────────────────────────────────

def check_license(silent=False):
    """
    Логика:
      1. Если offline cache свежий (<7d), signature OK и hwid совпадает с текущим
         (stable или legacy) → пускаем silent.
      2. Иначе online check whitelist:
         a. stable_hwid в whitelist → ok, write cache
         b. legacy_hwid в whitelist → ok, write cache (stable), warning о миграции
         c. ни одного → access denied (показываем оба HWID)
      3. Network error:
         - если cache валиден → пускаем + warning "offline mode"
         - иначе error
    """
    stable_hwid = get_stable_hwid()
    legacy_hwid = get_legacy_hwid()

    # 1. OFFLINE CACHE FAST-PATH
    cached_hwid, cached_ts = _read_cache()
    if cached_hwid and cached_ts:
        age = time.time() - cached_ts
        if age < _CACHE_TTL_SECONDS and cached_hwid in (stable_hwid, legacy_hwid):
            return True  # silent — cache валиден, online не дёргаем

    # 2. ONLINE CHECK
    try:
        cache_buster = f"?t={int(time.time())}"
        response = requests.get(LICENSE_URL + cache_buster, timeout=10)
        response.raise_for_status()

        # Парсим список (по строкам, без комментариев)
        allowed_hwids = []
        raw_text = response.text.strip()
        for line in raw_text.splitlines():
            cleaned = line.strip()
            if cleaned and not cleaned.startswith('#'):
                allowed_hwids.append(cleaned.upper())
        # На случай если файл без переносов строк — добавим весь текст как один ID
        if raw_text:
            full_upper = raw_text.upper()
            if full_upper not in allowed_hwids:
                allowed_hwids.append(full_upper)

        # 2a. stable HWID в whitelist
        if stable_hwid in allowed_hwids:
            _write_cache(stable_hwid)
            return True

        # 2b. legacy HWID в whitelist — пускаем + просим обновить
        if legacy_hwid in allowed_hwids:
            _write_cache(stable_hwid)  # кэшируем уже stable, чтобы при следующем запуске
                                       # warning не повторялся (cache hit → silent)
            if not silent:
                _show_migration_warning(stable_hwid, legacy_hwid)
            return True

        # 2c. Ни stable, ни legacy не найдены — access denied
        if not silent:
            _show_access_denied(stable_hwid, legacy_hwid)
        else:
            # silent режим (например периодическая проверка) — копируем в буфер тихо
            _copy_to_clipboard(stable_hwid)
        sys.exit(1)

    except requests.exceptions.RequestException as e:
        # Network error: если cache валиден и matches current HWID — пускаем
        if cached_hwid and cached_hwid in (stable_hwid, legacy_hwid):
            if not silent:
                _show_network_warning(cached_hwid, cached_ts, e)
            return True
        # Нет cache — fail
        if not silent:
            _show_network_error(e)
        sys.exit(1)

    except Exception as e:
        if not silent:
            _show_critical_error(e)
        sys.exit(1)


# ─── UI HELPERS ──────────────────────────────────────────────────────

def _copy_to_clipboard(text):
    """tkinter clipboard с fallback на pyperclip."""
    try:
        root = tk.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(text)
        root.update()
        root.destroy()
    except Exception:
        if HAS_PYPERCLIP:
            try:
                pyperclip.copy(text)
            except Exception:
                pass


def _show_migration_warning(stable_hwid, legacy_hwid):
    """Старый HWID в whitelist — попросить заменить на новый."""
    msg = (
        f"Лицензия активна (по старому HWID).\n\n"
        f"Старый HWID: {legacy_hwid}\n"
        f"Новый HWID: {stable_hwid}\n\n"
        f"Отправьте новый HWID администратору для миграции whitelist'а.\n"
        f"После замены этот warning исчезнет."
    )
    _copy_to_clipboard(stable_hwid)
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showwarning("Лицензия: миграция HWID", msg)
        root.destroy()
    except Exception:
        pass


def _show_access_denied(stable_hwid, legacy_hwid):
    """Ни stable, ни legacy не в whitelist."""
    msg = (
        f"Доступ запрещен!\n\n"
        f"Новый HWID (стабильный): {stable_hwid}\n"
        f"Старый HWID (MAC-based): {legacy_hwid}\n\n"
        f"Новый HWID скопирован в буфер обмена.\n"
        f"Отправьте его администратору для активации."
    )
    _copy_to_clipboard(stable_hwid)
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showerror("Доступ запрещен", msg)
        root.destroy()
    except Exception:
        pass


def _show_network_warning(cached_hwid, cached_ts, e):
    """Cache валиден, сеть упала — пускаем с warning."""
    age_hours = int((time.time() - cached_ts) / 3600)
    remaining_hours = int((_CACHE_TTL_SECONDS - (time.time() - cached_ts)) / 3600)
    msg = (
        f"Нет соединения с сервером лицензий.\n\n"
        f"Используется offline-cache (возраст ~{age_hours}ч).\n"
        f"Cache валиден ещё ~{remaining_hours}ч.\n\n"
        f"Подключитесь к интернету для обновления лицензии.\n\n"
        f"Ошибка сети: {str(e)[:200]}"
    )
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showwarning("Offline режим", msg)
        root.destroy()
    except Exception:
        pass


def _show_network_error(e):
    """Cache недоступен И сеть упала — fail."""
    msg = (
        f"Ошибка сети при проверке лицензии!\n\n"
        f"Не удалось подключиться к серверу + offline-cache недоступен.\n"
        f"Проверьте интернет-соединение.\n\n"
        f"Ошибка: {str(e)[:200]}"
    )
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showerror("Ошибка сети", msg)
        root.destroy()
    except Exception:
        pass


def _show_critical_error(e):
    """Любая неожиданная ошибка."""
    msg = f"Критическая ошибка проверки лицензии!\n\nОшибка: {str(e)[:300]}"
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        messagebox.showerror("Ошибка безопасности", msg)
        root.destroy()
    except Exception:
        pass
