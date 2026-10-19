"""
winutil.py — платформенные утилиты для мастер-консоли и агента.

Содержит:
  * IS_WINDOWS                — флаг текущей ОС;
  * primary_ip()             — основной IPv4 машины;
  * system_summary()         — краткая строка об ОС;
  * verify_windows_login()   — проверка локального логина/пароля через LogonUser;
  * dpapi_protect/unprotect  — шифрование секретов «под текущего пользователя» (DPAPI);
  * connect_rdp/clear_rdp    — подключение к удалённому столу через mstsc + cmdkey.

Все Windows-специфичные вызовы сделаны через ctypes, чтобы не тянуть pywin32.
На не-Windows (например, при разработке под Linux) функции деградируют
безопасно: проверка логина пропускается, DPAPI заменяется на base64 и т.п.
"""

import base64
import getpass
import platform
import socket
import subprocess
import sys

IS_WINDOWS = sys.platform.startswith("win")


# ---- Сведения о машине ----------------------------------------------------

def primary_ip():
    """Определить основной IPv4-адрес (тот, через который идёт трафик наружу)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Адрес никуда реально не отправляется — нужен лишь для выбора маршрута.
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def system_summary():
    """Короткое описание ОС, например 'Windows 10 (10.0.19045)'."""
    return f"{platform.system()} {platform.release()} ({platform.version()})"


def current_username():
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def machine_id():
    """
    Стабильный идентификатор, уникальный для каждой машины.

    На Windows — MachineGuid из реестра (создаётся при установке системы,
    не меняется при смене IP, перезагрузке и копировании папки программы).
    Это важно: если разворачивать агентов копированием папки, идентификатор
    всё равно будет у каждой машины свой, и они не будут выбивать друг друга
    с мастера.

    Запасной вариант — по MAC-адресу.
    """
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Cryptography", 0,
                    winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
                guid, _ = winreg.QueryValueEx(k, "MachineGuid")
            if guid:
                return "mg-" + str(guid).replace("-", "").lower()
        except OSError:
            pass
    import uuid
    return "mac-" + format(uuid.getnode(), "012x")


# ---- Проверка учётной записи (требование: вход только под Администратором) --

# Константы LogonUser
_LOGON32_LOGON_NETWORK = 3        # не создаёт интерактивную сессию — быстро и без следов
_LOGON32_PROVIDER_DEFAULT = 0


_TOKEN_GROUPS_CLASS = 2              # TokenGroups
_WIN_BUILTIN_ADMINISTRATORS_SID = 26  # WinBuiltinAdministratorsSid (S-1-5-32-544)

# Результаты проверки входа
LOGIN_OK = "ok"
LOGIN_BAD_PASSWORD = "bad_password"
LOGIN_NOT_ADMIN = "not_admin"


def _token_has_admin_group(token):
    """
    Есть ли в токене группа BUILTIN\\Administrators.

    Проверяем список групп токена напрямую, а не CheckTokenMembership: при
    UAC у обычных локальных админов группа присутствует с флагом «deny-only»,
    и CheckTokenMembership вернул бы False, хотя пользователь — администратор.
    """
    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.CreateWellKnownSid.argtypes = [
        ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD)]
    advapi32.CreateWellKnownSid.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL

    # SID группы Администраторов
    admin_sid = ctypes.create_string_buffer(68)  # SECURITY_MAX_SID_SIZE
    sid_size = wintypes.DWORD(len(admin_sid))
    if not advapi32.CreateWellKnownSid(_WIN_BUILTIN_ADMINISTRATORS_SID, None,
                                       admin_sid, ctypes.byref(sid_size)):
        return False

    # Список групп токена
    needed = wintypes.DWORD(0)
    advapi32.GetTokenInformation(token, _TOKEN_GROUPS_CLASS, None, 0,
                                 ctypes.byref(needed))
    buf = ctypes.create_string_buffer(needed.value)
    if not advapi32.GetTokenInformation(token, _TOKEN_GROUPS_CLASS, buf,
                                        needed, ctypes.byref(needed)):
        return False

    count = ctypes.cast(buf, ctypes.POINTER(wintypes.DWORD))[0]
    # Массив SID_AND_ATTRIBUTES начинается после GroupCount с учётом выравнивания
    offset = ctypes.sizeof(ctypes.c_void_p)
    groups = (SID_AND_ATTRIBUTES * count).from_buffer(buf, offset)
    return any(advapi32.EqualSid(g.Sid, admin_sid) for g in groups)


def check_admin_login(username, password, domain="."):
    """
    Проверить логин/пароль локальной учётной записи Windows И то, что она
    входит в группу Администраторы.

    domain="." означает локальную машину.
    Возвращает LOGIN_OK / LOGIN_BAD_PASSWORD / LOGIN_NOT_ADMIN.

    На не-Windows всегда LOGIN_OK (только для разработки).
    """
    if not IS_WINDOWS:
        return LOGIN_OK

    import ctypes
    from ctypes import wintypes

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    advapi32.LogonUserW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
        wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.LogonUserW.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    ok = advapi32.LogonUserW(
        username, domain, password,
        _LOGON32_LOGON_NETWORK, _LOGON32_PROVIDER_DEFAULT, ctypes.byref(token),
    )
    if not ok:
        return LOGIN_BAD_PASSWORD
    try:
        return LOGIN_OK if _token_has_admin_group(token) else LOGIN_NOT_ADMIN
    finally:
        kernel32.CloseHandle(token)


def verify_windows_login(username, password, domain="."):
    """Совместимость: True только для верного пароля учётки-администратора."""
    return check_admin_login(username, password, domain) == LOGIN_OK


# ---- DPAPI: защита секретов (токена) на диске -----------------------------

def dpapi_protect(data: bytes) -> bytes:
    """Зашифровать данные под текущего пользователя Windows (DPAPI)."""
    if not IS_WINDOWS:
        # Запасной вариант для разработки — НЕ криптостойкий.
        return b"b64:" + base64.b64encode(data)

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    blob_in = DATA_BLOB(len(data), ctypes.cast(
        ctypes.create_string_buffer(data, len(data)),
        ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not crypt32.CryptProtectData(ctypes.byref(blob_in), None, None, None,
                                    None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def dpapi_unprotect(blob: bytes) -> bytes:
    """Расшифровать данные, ранее защищённые dpapi_protect()."""
    if not IS_WINDOWS or blob.startswith(b"b64:"):
        return base64.b64decode(blob[4:]) if blob.startswith(b"b64:") else blob

    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    blob_in = DATA_BLOB(len(blob), ctypes.cast(
        ctypes.create_string_buffer(blob, len(blob)),
        ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None, None,
                                      None, 0, ctypes.byref(blob_out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


# ---- Сеансы пользователей (для теневого подключения) ----------------------

# Состояния сеанса WTS
_WTS_ACTIVE = 0
_WTS_DISCONNECTED = 4
_WTS_STATE_NAMES = {0: "активен", 1: "подключается", 2: "подключён",
                    3: "скрыт", 4: "отключён", 5: "простаивает",
                    6: "ожидание", 7: "сброшен", 8: "вниз", 9: "инициализация"}
_WTS_CURRENT_SERVER = 0
_WTS_UserName = 5


def list_sessions():
    """
    Список сеансов пользователей на этой машине (через WTS API).
    Возвращает [{id, user, state, state_name, active}], отсортированный так,
    что активный сеанс реального пользователя идёт первым.

    На не-Windows возвращает пустой список.
    """
    if not IS_WINDOWS:
        return []

    import ctypes
    from ctypes import wintypes

    wtsapi = ctypes.WinDLL("wtsapi32", use_last_error=True)

    class WTS_SESSION_INFO(ctypes.Structure):
        _fields_ = [("SessionId", wintypes.DWORD),
                    ("pWinStationName", wintypes.LPWSTR),
                    ("State", ctypes.c_int)]

    wtsapi.WTSEnumerateSessionsW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(ctypes.POINTER(WTS_SESSION_INFO)),
        ctypes.POINTER(wintypes.DWORD)]
    wtsapi.WTSEnumerateSessionsW.restype = wintypes.BOOL
    wtsapi.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD)]
    wtsapi.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wtsapi.WTSFreeMemory.argtypes = [ctypes.c_void_p]

    p_sessions = ctypes.POINTER(WTS_SESSION_INFO)()
    count = wintypes.DWORD(0)
    if not wtsapi.WTSEnumerateSessionsW(_WTS_CURRENT_SERVER, 0, 1,
                                        ctypes.byref(p_sessions),
                                        ctypes.byref(count)):
        return []

    # Для теневого подключения нужен сеанс реального пользователя: состояние
    # Active / Connected / Disconnected и номер не 0 (0 — служебный «Services»).
    # Имя пользователя НЕ обязательно: оно только для отображения, и если его
    # не удалось прочитать, сеанс всё равно годится.
    _USABLE_STATES = {_WTS_ACTIVE, 1, _WTS_DISCONNECTED}  # 0, 1, 4

    out = []
    try:
        for i in range(count.value):
            s = p_sessions[i]
            sid = int(s.SessionId)
            state = int(s.State)
            station = s.pWinStationName or ""
            if sid == 0 or state not in _USABLE_STATES:
                continue
            if station.lower().startswith("services"):
                continue

            # Имя пользователя — по возможности (может не прочитаться).
            buf = wintypes.LPWSTR()
            blen = wintypes.DWORD(0)
            user = ""
            if wtsapi.WTSQuerySessionInformationW(
                    _WTS_CURRENT_SERVER, s.SessionId, _WTS_UserName,
                    ctypes.byref(buf), ctypes.byref(blen)):
                user = buf.value or ""
                wtsapi.WTSFreeMemory(buf)

            out.append({
                "id": sid,
                "user": user,
                "station": station,
                "state": state,
                "state_name": _WTS_STATE_NAMES.get(state, str(state)),
                "active": state == _WTS_ACTIVE,
            })
    finally:
        wtsapi.WTSFreeMemory(p_sessions)

    # Активные — первыми, затем отключённые.
    out.sort(key=lambda d: (not d["active"], d["state"] != _WTS_DISCONNECTED))
    return out


def pick_target_session(sessions):
    """Выбрать сеанс для теневого подключения: активный пользователь важнее."""
    for s in sessions:
        if s.get("active"):
            return s
    for s in sessions:
        if s.get("state") == _WTS_DISCONNECTED:
            return s
    return sessions[0] if sessions else None


# ---- Удалённый рабочий стол (встроенный RDP) ------------------------------

def connect_rdp(ip, username, password, domain=None):
    """
    Подключиться к удалённому столу машины по IP.

    Сохраняет учётные данные во встроенном хранилище Windows (cmdkey), затем
    запускает стандартный клиент mstsc. Так пользователю не нужно вводить
    пароль вручную — вход идёт под той же учётной записью Администратора.

    Возвращает запущенный процесс mstsc (Popen) либо бросает ошибку.
    """
    if not IS_WINDOWS:
        raise RuntimeError("RDP доступен только на Windows")

    store_rdp_credentials(ip, username, password, domain)
    return subprocess.Popen(["mstsc", f"/v:{ip}"])


def store_rdp_credentials(ip, username, password, domain=None):
    """Положить логин/пароль в хранилище Windows для RDP к этому хосту (cmdkey)."""
    if not IS_WINDOWS:
        return
    user = f"{domain}\\{username}" if domain else username
    subprocess.run(
        ["cmdkey", f"/generic:TERMSRV/{ip}", f"/user:{user}", f"/pass:{password}"],
        check=False, capture_output=True, text=True,
    )


def shadow_session(ip, session_id, control=True):
    """
    Теневое подключение к существующему сеансу пользователя: вы видите тот же
    экран, что и человек, и (при control=True) управляете им, НЕ выкидывая его.

    /noConsentPrompt — без запроса согласия у пользователя (работает, если на
    целевой машине политика Shadow = 2, что агент настраивает при установке).
    Подключение всегда идёт под текущей учётной записью администратора (SSO).
    """
    if not IS_WINDOWS:
        raise RuntimeError("Теневой сеанс доступен только на Windows")
    args = ["mstsc", f"/v:{ip}", f"/shadow:{session_id}", "/noConsentPrompt"]
    if control:
        args.append("/control")
    return subprocess.Popen(args)


def enable_shadow_policy():
    """
    Разрешить теневое подключение без запроса согласия (Shadow = 2) и открыть
    в брандмауэре правила для теневого сеанса. Выполняется на агенте при
    установке, нужны права администратора. Возвращает (успех, сообщение).
    """
    if not IS_WINDOWS:
        return False, "только для Windows"
    # Shadow=2 — полный контроль без согласия пользователя.
    reg = subprocess.run(
        ["reg", "add",
         r"HKLM\SOFTWARE\Policies\Microsoft\Windows NT\Terminal Services",
         "/v", "Shadow", "/t", "REG_DWORD", "/d", "2", "/f"],
        capture_output=True, text=True)
    # Правила брандмауэра для теневого подключения (имена не локализованы).
    fw = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Enable-NetFirewallRule -Name "
         "'RemoteDesktop-Shadow-In-TCP','RemoteDesktop-In-TCP',"
         "'RemoteDesktop-UserMode-In-TCP','RemoteDesktop-UserMode-In-UDP' "
         "-ErrorAction SilentlyContinue"],
        capture_output=True, text=True)
    ok = reg.returncode == 0
    return ok, ("теневой сеанс разрешён" if ok
                else (reg.stderr or "").strip())


def clear_rdp(ip):
    """Удалить ранее сохранённые RDP-данные для хоста (гигиена безопасности)."""
    if not IS_WINDOWS:
        return
    subprocess.run(["cmdkey", f"/delete:TERMSRV/{ip}"],
                   check=False, capture_output=True, text=True)


def builtin_admin_name():
    """
    Имя встроенной учётки администратора (RID 500): «Администратор» в русской
    Windows, «Administrator» в английской. При ошибке — «Администратор».
    """
    if not IS_WINDOWS:
        return "Administrator"
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
             "(Get-LocalUser | Where-Object { $_.SID -like '*-500' }).Name"],
            capture_output=True, timeout=20)
        name = res.stdout.decode("utf-8", errors="replace").strip()
        return name or "Администратор"
    except Exception:
        return "Администратор"


def enable_rdp_server():
    """
    Включить входящие RDP-подключения на этой машине (выполняется на агенте
    при установке, нужны права администратора):
      * снять запрет fDenyTSConnections;
      * открыть правила брандмауэра группы «Удалённый рабочий стол».
    Возвращает (успех, сообщение).
    """
    if not IS_WINDOWS:
        return False, "только для Windows"
    reg = subprocess.run(
        ["reg", "add",
         r"HKLM\SYSTEM\CurrentControlSet\Control\Terminal Server",
         "/v", "fDenyTSConnections", "/t", "REG_DWORD", "/d", "0", "/f"],
        capture_output=True, text=True)
    # Имя группы правил локализовано, поэтому используем независимый от языка
    # идентификатор группы через PowerShell.
    fw = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Enable-NetFirewallRule -Group '@FirewallAPI.dll,-28752'"],
        capture_output=True, text=True)
    ok = reg.returncode == 0 and fw.returncode == 0
    msg = "RDP включён" if ok else (reg.stderr + fw.stderr).strip()
    return ok, msg
