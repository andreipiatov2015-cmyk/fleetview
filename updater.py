"""
updater.py — проверка и скачивание обновлений с GitHub Releases.

Публичный репозиторий, поэтому токен не нужен: берём последний релиз через
GitHub API и скачиваем вложения (assets). Только стандартная библиотека.

Схема релиза (создаётся автоматически через GitHub Actions по тегу vX.Y.Z):
  тег            v1.2.0
  asset          FleetViewAgent.exe        — для самообновления агентов
  asset          FleetView-console.zip     — исходники консоли для мастера

Самообновление:
  * агент: agent_self_update() — тихо заменяет свой exe и перезапускается;
  * мастер: console_fetch_update() скачивает zip, apply_console_update()
    подменяет файлы и перезапускает консоль.
"""

import json
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.request

from version import VERSION, GITHUB_OWNER, GITHUB_REPO

API_LATEST = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
              "/releases/latest")
_UA = "FleetView-Updater"
_TIMEOUT = 30


# ---- сравнение версий -----------------------------------------------------

def parse_version(text):
    """'v1.2.3' или '1.2.3' -> (1, 2, 3). Нераспознанное -> (0,)."""
    text = (text or "").strip().lstrip("vV")
    parts = []
    for chunk in text.split("."):
        num = ""
        for ch in chunk:
            if ch.isdigit():
                num += ch
            else:
                break
        if num == "":
            break
        parts.append(int(num))
    return tuple(parts) if parts else (0,)


def is_newer(candidate, current=VERSION):
    """True, если версия candidate строго новее current."""
    return parse_version(candidate) > parse_version(current)


# ---- запросы к GitHub -----------------------------------------------------

def _opener():
    # На машинах пользователей прокси нет (прямой выход). В средах с прокси
    # urllib сам возьмёт его из переменных окружения.
    ctx = ssl.create_default_context()
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.ProxyHandler(urllib.request.getproxies()))


def get_latest_release():
    """
    Вернуть сведения о последнем релизе:
      {"version": "1.2.0", "notes": "...", "assets": {name: url}}
    или None, если релизов нет / нет сети.
    """
    req = urllib.request.Request(API_LATEST, headers={
        "User-Agent": _UA, "Accept": "application/vnd.github+json"})
    try:
        with _opener().open(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if data.get("draft") or not data.get("tag_name"):
        return None
    assets = {a["name"]: a["browser_download_url"]
              for a in data.get("assets", [])}
    return {"version": data["tag_name"].lstrip("vV"),
            "notes": data.get("body") or "",
            "assets": assets}


def download(url, dest, chunk=256 * 1024):
    """Скачать url в файл dest (потоково). Бросает исключение при ошибке."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    tmp = dest + ".part"
    with _opener().open(req, timeout=_TIMEOUT) as resp, open(tmp, "wb") as f:
        while True:
            buf = resp.read(chunk)
            if not buf:
                break
            f.write(buf)
    os.replace(tmp, dest)
    return dest


# ---- самообновление агента (собранный exe) --------------------------------

_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


def cleanup_old_exe():
    """Удалить остаток прошлого обновления (FleetViewAgent.exe.old)."""
    if getattr(sys, "frozen", False):
        old = sys.executable + ".old"
        if os.path.exists(old):
            try:
                os.remove(old)
            except OSError:
                pass


def agent_self_update(log=print):
    """
    Проверить релиз и, если он новее, заменить свой exe и перезапуститься.
    Возвращает True, если обновление запущено (процесс должен завершиться).

    Работает только для собранного exe (frozen). В виде скрипта пропускается.
    """
    if not getattr(sys, "frozen", False):
        return False
    rel = get_latest_release()
    if not rel or not is_newer(rel["version"]):
        return False
    url = rel["assets"].get("FleetViewAgent.exe")
    if not url:
        return False

    exe = sys.executable
    new = exe + ".new"
    old = exe + ".old"
    log(f"Найдено обновление {rel['version']} (сейчас {VERSION}), скачиваю…")
    try:
        download(url, new)
    except Exception as exc:  # noqa: BLE001
        log(f"Не удалось скачать обновление: {exc}")
        return False

    try:
        if os.path.exists(old):
            os.remove(old)
        os.rename(exe, old)      # Windows разрешает переименовать запущенный exe
        os.rename(new, exe)
    except OSError as exc:
        log(f"Не удалось применить обновление: {exc}")
        return False

    log(f"Обновление установлено, перезапускаюсь на {rel['version']}")
    try:
        subprocess.Popen([exe], creationflags=_DETACHED, close_fds=True)
    except OSError:
        # Запасной путь — через планировщик.
        subprocess.Popen(["schtasks", "/Run", "/TN", "FleetViewAgent"])
    return True


# ---- обновление мастера (набор файлов) ------------------------------------

def console_fetch_update(dest_dir):
    """
    Скачать и распаковать свежий пакет консоли в dest_dir/_update_staging.
    Возвращает (version, notes, staging_dir) или None.
    """
    rel = get_latest_release()
    if not rel or not is_newer(rel["version"]):
        return None
    url = rel["assets"].get("FleetView-console.zip")
    if not url:
        return None
    import tempfile
    import zipfile

    staging = os.path.join(dest_dir, "_update_staging")
    zpath = os.path.join(tempfile.gettempdir(), "FleetView-console.zip")
    download(url, zpath)

    import shutil
    if os.path.exists(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging, exist_ok=True)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(staging)
    # В архиве может быть вложенная папка fleetview/ — разворачиваем.
    entries = os.listdir(staging)
    if len(entries) == 1 and os.path.isdir(os.path.join(staging, entries[0])):
        staging = os.path.join(staging, entries[0])
    try:
        os.remove(zpath)
    except OSError:
        pass
    return rel["version"], rel["notes"], staging


def apply_console_update(staging_dir, install_dir, restart_cmd):
    """
    Запустить отдельный процесс-помощник, который дождётся выхода консоли,
    скопирует файлы из staging_dir в install_dir и снова запустит консоль.
    Конфиг консоли лежит в %APPDATA%, его копирование не затрагивает.
    """
    helper = os.path.join(install_dir, "_apply_update.py")
    code = _HELPER_TEMPLATE.format(
        ppid=os.getpid(),
        staging=repr(staging_dir),
        install=repr(install_dir),
        restart=repr(restart_cmd),
    )
    with open(helper, "w", encoding="utf-8") as f:
        f.write(code)
    pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    runner = pyw if os.path.exists(pyw) else sys.executable
    subprocess.Popen([runner, helper], creationflags=_DETACHED, close_fds=True)


# Помощник выполняется уже после закрытия консоли, отдельным процессом.
_HELPER_TEMPLATE = '''# -*- coding: utf-8 -*-
import os, sys, time, shutil, subprocess

PPID = {ppid}
STAGING = {staging}
INSTALL = {install}
RESTART = {restart}
SKIP = {{"console.json", "agent_config.json", "_apply_update.py",
        "_update_staging", "__pycache__", "agent.log"}}

# Ждём, пока старая консоль закроется (до ~30 с).
for _ in range(60):
    try:
        os.kill(PPID, 0)
        time.sleep(0.5)
    except OSError:
        break

# Копируем новые файлы поверх, с повторами на случай блокировок.
for root, dirs, files in os.walk(STAGING):
    dirs[:] = [d for d in dirs if d not in SKIP]
    rel = os.path.relpath(root, STAGING)
    target = INSTALL if rel == "." else os.path.join(INSTALL, rel)
    os.makedirs(target, exist_ok=True)
    for name in files:
        if name in SKIP:
            continue
        src = os.path.join(root, name)
        dst = os.path.join(target, name)
        for attempt in range(10):
            try:
                shutil.copy2(src, dst)
                break
            except OSError:
                time.sleep(0.5)

shutil.rmtree(STAGING, ignore_errors=True)
subprocess.Popen(RESTART, cwd=INSTALL)
'''
