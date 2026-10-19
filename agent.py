"""
agent.py — агент FleetView для управляемой машины (Windows).

Что делает:
  * подключается к мастер-консоли по TCP, присылает токен и сведения о себе;
  * держит соединение живым (heartbeat), автоматически переподключается;
  * принимает команды и выполняет их (PowerShell/CMD), возвращая вывод;
  * умеет устанавливать себя в автозапуск под учётной записью Администратора,
    чтобы все действия выполнялись с правами администратора.

Запуск:
  python agent.py --configure
      интерактивно спросить адрес мастера / порт / токен и сохранить конфиг.

  python agent.py --master 192.168.1.10 --port 7420 --token XXXX [--install]
      задать параметры из командной строки; --install добавит задачу автозапуска.

  python agent.py
      обычный запуск по сохранённому конфигу (так агент работает постоянно).

Конфиг (адрес мастера, порт, токен, стабильный agent_id) хранится в
agent_config.json рядом со скриптом. Пароль администратора агентом НЕ хранится:
он передаётся планировщику задач один раз при установке, дальше задача сама
запускается под нужной учётной записью.
"""

import argparse
import getpass
import json
import os
import socket
import subprocess
import sys
import threading
import time
import uuid

import protocol
import updater
import winutil
from version import VERSION

# Как часто агент проверяет наличие обновлений (секунды).
UPDATE_CHECK_INTERVAL = 30 * 60

# Агент может работать как скрипт (python agent.py) или как собранный
# FleetViewAgent.exe (PyInstaller) — тогда файлы лежат рядом с .exe.
FROZEN = getattr(sys, "frozen", False)
BASE_DIR = os.path.dirname(sys.executable if FROZEN
                           else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "agent_config.json")
LOG_PATH = os.path.join(BASE_DIR, "agent.log")

# Кодировка вывода системных утилит (schtasks, reg) в русской Windows — OEM 866.
_SYS_ENC = "cp866" if winutil.IS_WINDOWS else "utf-8"


def log(*parts):
    """Печать + запись в agent.log (в фоне окна нет, лог — единственный след)."""
    line = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        # Лог не даём разрастись: при 1 МБ начинаем заново.
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.remove(LOG_PATH)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass

HEARTBEAT_INTERVAL = 10          # секунд между heartbeat
RECONNECT_BACKOFF = [2, 3, 5, 8, 13]   # нарастающая пауза при переподключении
COMMAND_TIMEOUT = 300            # максимум секунд на одну команду

_stop = threading.Event()


# ---- конфиг ---------------------------------------------------------------

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return None
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    print(f"Конфиг сохранён: {CONFIG_PATH}")


def ensure_agent_id(cfg):
    # Идентификатор привязан к машине, а не к файлу конфигурации. Так два ПК
    # с одинаковым (скопированным) конфигом всё равно получат разные ID и не
    # будут выбивать друг друга с мастера.
    cfg["agent_id"] = winutil.machine_id()
    return cfg


# Фиксированный локальный порт-замок: не даёт запустить второй агент на ПК.
_SINGLE_INSTANCE_PORT = 50912
_lock_sock = None


def acquire_single_instance(wait_seconds=10):
    """
    Гарантировать, что на машине работает только один агент. Возвращает True,
    если это единственный экземпляр. Замок автоматически снимается при выходе
    процесса (ОС освобождает порт).

    Несколько секунд повторяем попытки: при самообновлении новый процесс
    стартует, пока старый ещё завершается, — даём старому освободить замок.
    """
    global _lock_sock
    import socket as _socket
    deadline = time.time() + wait_seconds
    while True:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", _SINGLE_INSTANCE_PORT))
            s.listen(1)
            _lock_sock = s  # держим ссылку до конца жизни процесса
            return True
        except OSError:
            s.close()
            if time.time() >= deadline:
                return False
            if _stop.wait(1.0):
                return False


# ---- выполнение команд ----------------------------------------------------

def _build_args(command, shell):
    """Собрать аргументы запуска по выбранной оболочке."""
    if shell == "auto":
        shell = "powershell" if winutil.IS_WINDOWS else "bash"
    if shell == "powershell":
        # Принудительно UTF-8, иначе кириллица придёт в OEM-866 и исказится.
        prefix = ("[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                  "$OutputEncoding=[Text.Encoding]::UTF8;")
        return ["powershell", "-NoProfile", "-NonInteractive",
                "-ExecutionPolicy", "Bypass", "-Command", prefix + command]
    if shell == "cmd":
        return ["cmd", "/c", f"chcp 65001>nul & {command}"]
    # bash — для не-Windows / разработки
    return ["bash", "-lc", command]


# Не показывать консольное окно при запуске команд из фонового агента.
_NO_WINDOW = 0x08000000 if winutil.IS_WINDOWS else 0  # CREATE_NO_WINDOW


def execute_command(command, shell):
    """Выполнить команду и вернуть (exit_code, stdout, stderr)."""
    try:
        proc = subprocess.run(
            _build_args(command, shell),
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=COMMAND_TIMEOUT,
            creationflags=_NO_WINDOW,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        err = proc.stderr.decode("utf-8", errors="replace")
        return proc.returncode, out, err
    except subprocess.TimeoutExpired:
        return 124, "", f"превышен таймаут {COMMAND_TIMEOUT} с"
    except FileNotFoundError as exc:
        return 127, "", f"оболочка не найдена: {exc}"
    except Exception as exc:  # noqa: BLE001 — вернём как ошибку выполнения
        return 1, "", f"ошибка запуска: {exc}"


def _handle_run(sock, write_lock, msg):
    """Выполнить команду в отдельном потоке и отправить результат мастеру."""
    cmd_id = msg.get("cmd_id")
    command = msg.get("command", "")
    shell = msg.get("shell", "auto")
    code, out, err = execute_command(command, shell)
    result = {
        "type": protocol.MSG_RESULT,
        "cmd_id": cmd_id,
        "exit_code": code,
        "stdout": out,
        "stderr": err,
    }
    try:
        with write_lock:
            protocol.send_msg(sock, result)
    except (OSError, protocol.ProtocolError):
        pass  # соединение упало — переподключимся в основном цикле


# ---- основной цикл агента -------------------------------------------------

def _heartbeat_loop(sock, write_lock, stop_conn):
    while not stop_conn.is_set() and not _stop.is_set():
        if stop_conn.wait(HEARTBEAT_INTERVAL):
            return
        try:
            with write_lock:
                protocol.send_msg(sock, {"type": protocol.MSG_HEARTBEAT})
        except (OSError, protocol.ProtocolError):
            stop_conn.set()
            return


def _session(cfg):
    """Одна попытка соединения: от подключения до обрыва."""
    host = cfg["master_host"]
    port = int(cfg["master_port"])
    sock = socket.create_connection((host, port), timeout=10)
    write_lock = threading.Lock()
    stop_conn = threading.Event()
    try:
        # Рукопожатие
        protocol.send_msg(sock, {
            "type": protocol.MSG_HELLO,
            "token": cfg["token"],
            "agent_id": cfg["agent_id"],
            "hostname": socket.gethostname(),
            "username": winutil.current_username(),
            "os": winutil.system_summary(),
            "ip": winutil.primary_ip(),
            "version": VERSION,
        })
        reply = protocol.recv_msg(sock)
        if reply.get("type") != protocol.MSG_HELLO_OK:
            log("Мастер отклонил подключение:", reply.get("msg"))
            _stop.wait(10)
            return

        log(f"Подключено к мастеру {host}:{port}")
        hb = threading.Thread(target=_heartbeat_loop,
                              args=(sock, write_lock, stop_conn), daemon=True)
        hb.start()

        while not _stop.is_set():
            msg = protocol.recv_msg(sock)
            mtype = msg.get("type")
            if mtype == protocol.MSG_RUN:
                threading.Thread(target=_handle_run,
                                 args=(sock, write_lock, msg),
                                 daemon=True).start()
            elif mtype == protocol.MSG_GET_SESSIONS:
                try:
                    sessions = winutil.list_sessions()
                except Exception as exc:  # noqa: BLE001
                    import traceback
                    log("Не удалось получить список сеансов:", exc,
                        "\n" + traceback.format_exc())
                    sessions = []
                log("Запрошены сеансы, найдено:",
                    ", ".join(f"id={s['id']} {s.get('state_name','')} "
                              f"{s.get('user') or s.get('station','')}"
                              for s in sessions) or "нет")
                with write_lock:
                    protocol.send_msg(sock, {"type": protocol.MSG_SESSIONS,
                                             "sessions": sessions})
            elif mtype == protocol.MSG_PING:
                with write_lock:
                    protocol.send_msg(sock, {"type": protocol.MSG_PONG})
    finally:
        stop_conn.set()
        try:
            sock.close()
        except OSError:
            pass


def _update_loop():
    """Периодически проверяет обновления и тихо обновляет exe."""
    # Небольшая задержка после старта, чтобы не мешать первому подключению.
    if _stop.wait(60):
        return
    while not _stop.is_set():
        try:
            if updater.agent_self_update(log=log):
                # Новая копия уже запущена отдельным процессом — выходим.
                _stop.set()
                os._exit(0)
        except Exception as exc:  # noqa: BLE001
            log("Ошибка проверки обновлений:", exc)
        _stop.wait(UPDATE_CHECK_INTERVAL)


def run_forever(cfg):
    log(f"Агент {cfg['agent_id'][:8]} v{VERSION} запущен. Мастер: "
        f"{cfg['master_host']}:{cfg['master_port']}")
    updater.cleanup_old_exe()  # убрать остаток прошлого обновления
    threading.Thread(target=_update_loop, name="updater", daemon=True).start()
    attempt = 0
    while not _stop.is_set():
        try:
            _session(cfg)
            attempt = 0  # успешная сессия — сбрасываем задержку
        except (OSError, protocol.ProtocolError) as exc:
            delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
            # Пишем в лог только первую ошибку серии, чтобы не засорять его.
            if attempt == 0:
                log(f"Нет связи с мастером ({exc}); пробую переподключаться")
            attempt += 1
            _stop.wait(delay)


# ---- установка в автозапуск под администратором ---------------------------

def install_startup_task(admin_user):
    """
    Создать задачу планировщика Windows, запускающую агента при старте системы
    под указанной учётной записью Администратора (пароль спросим безопасно).
    Так все команды агента выполняются с правами администратора.
    """
    if not winutil.IS_WINDOWS:
        print("Установка задачи доступна только на Windows.")
        return

    password = getpass.getpass(f"Пароль учётной записи {admin_user} "
                               "(символы не отображаются): ")
    if FROZEN:
        # Собранный FleetViewAgent.exe — Python на машине не нужен.
        run_cmd = f'"{sys.executable}"'
    else:
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable  # запасной вариант
        run_cmd = f'"{pythonw}" "{os.path.abspath(__file__)}"'

    cmd = [
        "schtasks", "/Create", "/TN", "FleetViewAgent",
        "/TR", run_cmd,
        "/SC", "ONSTART",
        "/RU", admin_user,
        "/RP", password,
        "/RL", "HIGHEST",   # запуск с наивысшими правами
        "/F",               # перезаписать, если уже есть
    ]
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding=_SYS_ENC, errors="replace")
    if res.returncode == 0:
        log("Задача автозапуска 'FleetViewAgent' создана. "
            "Агент будет стартовать при загрузке под", admin_user)
        # Сразу запускаем, чтобы не ждать перезагрузки.
        subprocess.run(["schtasks", "/Run", "/TN", "FleetViewAgent"],
                       capture_output=True)
    else:
        log("ОШИБКА: не удалось создать задачу:",
            res.stderr.strip() or res.stdout.strip())
        print("Проверьте имя учётной записи и пароль. В русской Windows "
              "встроенная учётка называется «Администратор».")
        return

    # Чтобы с мастера можно было подключиться к рабочему столу.
    ok, msg = winutil.enable_rdp_server()
    log("Удалённый рабочий стол:", msg)
    ok2, msg2 = winutil.enable_shadow_policy()
    log("Теневой сеанс:", msg2)
    print("\nГотово. Через несколько секунд компьютер появится в консоли мастера.")
    print(f"Если не появится — смотрите журнал: {LOG_PATH}")


# ---- CLI ------------------------------------------------------------------

def check_master(cfg):
    """Пробное рукопожатие с мастером. Возвращает (успех, пояснение)."""
    try:
        sock = socket.create_connection(
            (cfg["master_host"], int(cfg["master_port"])), timeout=5)
    except OSError as exc:
        return False, (f"не удалось подключиться ({exc}).\n"
                       "  Проверьте: IP мастера верный, консоль на мастере "
                       "запущена, на мастере открыт порт "
                       f"{cfg['master_port']} в брандмауэре.")
    try:
        protocol.send_msg(sock, {
            "type": protocol.MSG_HELLO, "token": cfg["token"],
            "agent_id": cfg["agent_id"], "hostname": socket.gethostname(),
            "username": winutil.current_username(),
            "os": winutil.system_summary(), "ip": winutil.primary_ip(),
        })
        reply = protocol.recv_msg(sock)
    except (OSError, protocol.ProtocolError) as exc:
        return False, f"связь оборвалась ({exc})"
    finally:
        sock.close()
    if reply.get("type") == protocol.MSG_HELLO_OK:
        return True, "связь с мастером есть, токен принят"
    return False, (f"мастер отказал: {reply.get('msg')}. "
                   "Скопируйте токен из консоли мастера ещё раз.")


def _ask(prompt, default=None):
    while True:
        value = input(prompt).strip()
        if value:
            return value
        if default is not None:
            return default
        print("  Значение обязательно.")


def interactive_configure():
    cfg = load_config() or {}
    print("=== Настройка агента FleetView ===\n")
    while True:
        cfg["master_host"] = _ask("IP-адрес мастер-компьютера: ")
        cfg["master_port"] = _ask("Порт [7420]: ", "7420")
        cfg["token"] = _ask("Токен (показан в консоли мастера): ")
        ensure_agent_id(cfg)

        print("\nПроверяю связь с мастером...")
        ok, msg = check_master(cfg)
        print(("  ОК: " if ok else "  ОШИБКА: ") + msg)
        if ok:
            break
        if input("Ввести данные заново? [Y/n]: ").strip().lower() == "n":
            break
        print()
    save_config(cfg)

    if winutil.IS_WINDOWS:
        ans = input("\nУстановить агента в автозапуск под администратором? "
                    "[Y/n]: ").strip().lower()
        if ans != "n":
            default_admin = winutil.builtin_admin_name()
            admin_user = _ask("Имя учётной записи администратора "
                              f"[{default_admin}]: ", default_admin)
            install_startup_task(admin_user)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Агент FleetView")
    parser.add_argument("--configure", action="store_true",
                        help="интерактивная настройка")
    parser.add_argument("--master", help="адрес мастер-консоли")
    parser.add_argument("--port", default="7420", help="порт мастера")
    parser.add_argument("--token", help="токен подключения")
    parser.add_argument("--install", action="store_true",
                        help="создать задачу автозапуска под администратором")
    parser.add_argument("--admin-user", default=None,
                        help="учётная запись для автозапуска "
                             "(по умолчанию — встроенный Администратор)")
    args = parser.parse_args(argv)

    if args.configure:
        interactive_configure()
        return

    if args.master and args.token:
        cfg = ensure_agent_id({
            "master_host": args.master,
            "master_port": args.port,
            "token": args.token,
            "agent_id": (load_config() or {}).get("agent_id"),
        })
        save_config(cfg)
        if args.install:
            install_startup_task(args.admin_user or winutil.builtin_admin_name())
            return

    cfg = load_config()
    if not cfg or not cfg.get("master_host"):
        if sys.stdin and sys.stdin.isatty():
            # Двойной щелчок по FleetViewAgent.exe без настройки — сразу
            # предлагаем настроить, а не молча закрываемся.
            interactive_configure()
            return
        log("Нет конфигурации. Запустите install_agent.bat")
        sys.exit(1)

    # Идентификатор всегда берём от машины (на случай скопированного конфига).
    ensure_agent_id(cfg)

    # Не допускаем второй экземпляр агента на этой машине — иначе два агента
    # с одним ID бесконечно выбивают друг друга с мастера.
    if not acquire_single_instance():
        log("Агент уже запущен на этой машине — второй экземпляр завершён.")
        return

    try:
        run_forever(cfg)
    except KeyboardInterrupt:
        _stop.set()
        print("\nАгент остановлен.")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — любую ошибку пишем в журнал
        import traceback
        log("АВАРИЙНОЕ ЗАВЕРШЕНИЕ:\n" + traceback.format_exc())
        raise
