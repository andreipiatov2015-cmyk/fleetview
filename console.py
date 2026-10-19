"""
console.py — мастер-консоль FleetView (графический интерфейс, PySide6).

Экран, похожий на AnyDesk: список подключённых сейчас компьютеров карточками,
поле для команды, запуск на выбранных / на всех, кнопка подключения к
удалённому рабочему столу. Вход в консоль — только после проверки
учётной записи локального Администратора (LogonUser).

Запуск:   python console.py

Конфиг консоли (порт, токен, имя администратора) хранится в
%APPDATA%\\FleetView\\console.json. Токен на диске защищён DPAPI.
Пароль администратора не сохраняется: он вводится при входе, проверяется и
хранится только в памяти текущего сеанса (используется при подключении по RDP).
"""

import base64
import json
import os
import secrets
import sys

from PySide6.QtCore import Qt, QObject, Signal, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFormLayout, QLineEdit, QLabel,
    QVBoxLayout, QHBoxLayout, QWidget, QPushButton, QCheckBox, QComboBox,
    QTextEdit, QScrollArea, QFrame, QMessageBox, QSpinBox, QSizePolicy,
)

import threading

import updater
import winutil
from netcore import MasterServer
from version import VERSION

# Папка установки консоли (для самообновления).
INSTALL_DIR = os.path.dirname(os.path.abspath(__file__))

APP_NAME = "FleetView"
DEFAULT_PORT = 7420


# ---- хранение конфига консоли ---------------------------------------------

def config_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(config_dir(), "console.json")


def load_console_config():
    p = config_path()
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        raw = json.load(f)
    # токен защищён DPAPI и сложен в base64
    try:
        blob = base64.b64decode(raw["token_blob"])
        raw["token"] = winutil.dpapi_unprotect(blob).decode("utf-8")
    except Exception:
        raw["token"] = None
    return raw


def save_console_config(port, token, admin_user):
    blob = winutil.dpapi_protect(token.encode("utf-8"))
    data = {
        "port": int(port),
        "admin_user": admin_user,
        "token_blob": base64.b64encode(blob).decode("ascii"),
    }
    with open(config_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ---- мост между сетевыми потоками и GUI -----------------------------------

class Bridge(QObject):
    """Сетевые колбэки приходят из других потоков; превращаем их в сигналы Qt."""
    agents_changed = Signal()
    result_received = Signal(str, dict)
    log_line = Signal(str)
    update_available = Signal(str, str)   # версия, описание
    update_absent = Signal(bool)          # True, если проверка по кнопке
    update_error = Signal(str)


def _check_login(parent, user, pwd):
    """Проверить учётку; показать понятную ошибку. True — можно входить."""
    status = winutil.check_admin_login(user, pwd)
    if status == winutil.LOGIN_OK:
        return True
    if status == winutil.LOGIN_NOT_ADMIN:
        QMessageBox.critical(parent, APP_NAME,
                             "Доступ запрещён: учётная запись не входит "
                             "в группу «Администраторы».")
    else:
        QMessageBox.critical(parent, APP_NAME,
                             "Доступ запрещён: неверный логин или пароль.")
    return False


# ---- диалог первичной настройки -------------------------------------------

class SetupDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — первичная настройка")
        self.token = secrets.token_hex(16)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            "Первый запуск. Задайте порт и укажите учётную запись локального\n"
            "Администратора — под ней будет работать консоль и подключения RDP."))

        form = QFormLayout()
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(DEFAULT_PORT)
        self.user = QLineEdit("Administrator")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        form.addRow("Порт сервера:", self.port)
        form.addRow("Администратор:", self.user)
        form.addRow("Пароль:", self.password)
        layout.addLayout(form)

        token_box = QHBoxLayout()
        token_field = QLineEdit(self.token)
        token_field.setReadOnly(True)
        copy_btn = QPushButton("Копировать")
        copy_btn.clicked.connect(
            lambda: QApplication.clipboard().setText(self.token))
        token_box.addWidget(QLabel("Токен:"))
        token_box.addWidget(token_field)
        token_box.addWidget(copy_btn)
        layout.addLayout(token_box)
        layout.addWidget(QLabel(
            "Этот токен нужно указать в агентах при их настройке."))

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_ok(self):
        user = self.user.text().strip()
        pwd = self.password.text()
        if not user or not pwd:
            QMessageBox.warning(self, APP_NAME, "Укажите логин и пароль.")
            return
        if not _check_login(self, user, pwd):
            return
        save_console_config(self.port.value(), self.token, user)
        self.admin_user = user
        self.admin_password = pwd
        self.accept()


# ---- диалог входа ----------------------------------------------------------

class LoginDialog(QDialog):
    def __init__(self, admin_user, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{APP_NAME} — вход")
        self.admin_user = admin_user
        self.admin_password = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Вход только для учётной записи Администратора."))
        form = QFormLayout()
        self.user = QLineEdit(admin_user)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.returnPressed.connect(self._on_ok)
        form.addRow("Пользователь:", self.user)
        form.addRow("Пароль:", self.password)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.password.setFocus()

    def _on_ok(self):
        user = self.user.text().strip()
        pwd = self.password.text()
        if not _check_login(self, user, pwd):
            return
        self.admin_user = user
        self.admin_password = pwd
        self.accept()


# ---- карточка одного компьютера -------------------------------------------

class AgentCard(QFrame):
    def __init__(self, info, on_rdp):
        super().__init__()
        self.agent_id = info.agent_id
        self.hostname = info.hostname
        self.setObjectName("agentCard")
        self.setFrameShape(QFrame.StyledPanel)

        root = QHBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)

        self.checkbox = QCheckBox()
        root.addWidget(self.checkbox)

        dot = QLabel("●")
        dot.setStyleSheet("color: #2ecc71; font-size: 16px;")
        root.addWidget(dot)

        text = QVBoxLayout()
        name = QLabel(info.hostname)
        name.setFont(QFont("", 11, QFont.Bold))
        sub = QLabel(f"{info.username} · {info.ip}")
        sub.setStyleSheet("color: #9aa4b2;")
        ver_txt = f" · агент v{info.version}" if info.version else ""
        osl = QLabel(info.os + ver_txt)
        osl.setStyleSheet("color: #6b7280; font-size: 10px;")
        text.addWidget(name)
        text.addWidget(sub)
        text.addWidget(osl)
        root.addLayout(text)
        root.addStretch(1)

        rdp_btn = QPushButton("Рабочий стол")
        rdp_btn.setObjectName("rdpBtn")
        rdp_btn.clicked.connect(lambda: on_rdp(info))
        root.addWidget(rdp_btn)

    def is_checked(self):
        return self.checkbox.isChecked()

    def set_checked(self, value):
        self.checkbox.setChecked(value)


# ---- главное окно ----------------------------------------------------------

class MainWindow(QWidget):
    def __init__(self, server, bridge, admin_user, admin_password, token, port):
        super().__init__()
        self.server = server
        self.bridge = bridge
        self.admin_user = admin_user
        self.admin_password = admin_password
        self._cards = {}  # agent_id -> AgentCard

        self.setWindowTitle(APP_NAME)
        self.resize(980, 640)

        root = QVBoxLayout(self)

        # Шапка
        header = QHBoxLayout()
        title = QLabel(APP_NAME)
        title.setFont(QFont("", 16, QFont.Bold))
        header.addWidget(title)
        ver = QLabel(f"v{VERSION}")
        ver.setStyleSheet("color: #6b7280;")
        header.addWidget(ver)
        header.addStretch(1)
        self.update_btn = QPushButton("Проверить обновления")
        self.update_btn.clicked.connect(lambda: self.check_updates(manual=True))
        header.addWidget(self.update_btn)
        self.status = QLabel("")
        self.status.setStyleSheet("color: #9aa4b2;")
        header.addWidget(self.status)
        root.addLayout(header)

        info_bar = QLabel(f"Слушаю порт {port} · токен: {token}")
        info_bar.setStyleSheet("color: #6b7280;")
        info_bar.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(info_bar)

        # Основная область: слева список машин, справа вывод
        body = QHBoxLayout()

        left = QVBoxLayout()
        tools = QHBoxLayout()
        select_all = QPushButton("Выбрать все")
        select_all.clicked.connect(lambda: self._set_all_checked(True))
        clear = QPushButton("Снять выбор")
        clear.clicked.connect(lambda: self._set_all_checked(False))
        tools.addWidget(select_all)
        tools.addWidget(clear)
        tools.addStretch(1)
        left.addLayout(tools)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.addStretch(1)
        self.scroll.setWidget(self.cards_host)
        self.scroll.setMinimumWidth(440)
        left.addWidget(self.scroll, 1)
        body.addLayout(left, 1)

        right = QVBoxLayout()
        right.addWidget(QLabel("Вывод команд"))
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setObjectName("output")
        right.addWidget(self.output, 1)
        clear_out = QPushButton("Очистить вывод")
        clear_out.clicked.connect(self.output.clear)
        right.addWidget(clear_out)
        body.addLayout(right, 1)
        root.addLayout(body, 1)

        # Нижняя панель: оболочка + команда + запуск
        bottom = QHBoxLayout()
        self.shell = QComboBox()
        self.shell.addItem("PowerShell", "powershell")
        self.shell.addItem("CMD", "cmd")
        bottom.addWidget(self.shell)
        self.cmd = QLineEdit()
        self.cmd.setPlaceholderText("Команда для выполнения…")
        self.cmd.returnPressed.connect(self.run_on_selected)
        bottom.addWidget(self.cmd, 1)
        run_sel = QPushButton("На выбранных")
        run_sel.clicked.connect(self.run_on_selected)
        run_all = QPushButton("На всех")
        run_all.setObjectName("runAll")
        run_all.clicked.connect(self.run_on_all)
        bottom.addWidget(run_sel)
        bottom.addWidget(run_all)
        root.addLayout(bottom)

        # Сигналы моста
        bridge.agents_changed.connect(self.refresh_agents)
        bridge.result_received.connect(self.show_result)
        bridge.log_line.connect(self.append_log)
        bridge.update_available.connect(self._on_update_available)
        bridge.update_absent.connect(self._on_update_absent)
        bridge.update_error.connect(self._on_update_error)

        self._apply_style()
        self.refresh_agents()
        # Тихая проверка обновлений при запуске.
        self.check_updates(manual=False)

    # ---- управление выбором/списком --------------------------------------

    def _set_all_checked(self, value):
        for card in self._cards.values():
            card.set_checked(value)

    def refresh_agents(self):
        agents = self.server.list_agents()
        # Не перестраиваем карточки, если состав и данные не менялись
        # (иначе таймер сбрасывал бы прокрутку и наведение каждые 5 с).
        snapshot = [(a.agent_id, a.hostname, a.username, a.ip, a.os,
                     a.peer_ip, a.version) for a in agents]
        if snapshot == getattr(self, "_snapshot", None):
            return
        self._snapshot = snapshot
        checked = {aid for aid, c in self._cards.items() if c.is_checked()}

        # удаляем старые карточки
        for aid, card in list(self._cards.items()):
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        self._cards.clear()

        for info in agents:
            card = AgentCard(info, self.connect_rdp)
            if info.agent_id in checked:
                card.set_checked(True)
            # вставляем перед растягивающим спейсером (последний элемент)
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self._cards[info.agent_id] = card

        self.status.setText(f"Онлайн: {len(agents)}")

    def _selected_ids(self):
        return [aid for aid, c in self._cards.items() if c.is_checked()]

    # ---- запуск команд ----------------------------------------------------

    def run_on_selected(self):
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, APP_NAME, "Не выбрано ни одной машины.")
            return
        self._run(ids)

    def run_on_all(self):
        if not self._cards:
            QMessageBox.information(self, APP_NAME, "Нет подключённых машин.")
            return
        self._run(None)

    def _run(self, ids):
        command = self.cmd.text().strip()
        if not command:
            return
        shell = self.shell.currentData()
        self.server.run_command(ids, command, shell)
        target = "всех" if ids is None else f"{len(ids)} выбранных"
        self.append_log(f"▶ Отправлено на {target}: {command}")

    # ---- вывод ------------------------------------------------------------

    def show_result(self, agent_id, result):
        card = self._cards.get(agent_id)
        host = _esc(card.hostname) if card else agent_id[:8]
        code = result.get("exit_code")
        color = "#2ecc71" if code == 0 else "#e74c3c"
        self.output.append(
            f'<div style="margin-top:8px;">'
            f'<b style="color:{color};">■ {host} (код {code})</b></div>')
        out = (result.get("stdout") or "").rstrip()
        err = (result.get("stderr") or "").rstrip()
        if out:
            self.output.append(f'<pre style="color:#d6dee8;">{_esc(out)}</pre>')
        if err:
            self.output.append(f'<pre style="color:#e59ba0;">{_esc(err)}</pre>')

    def append_log(self, line):
        self.output.append(f'<span style="color:#6b7280;">{_esc(line)}</span>')

    # ---- RDP --------------------------------------------------------------

    def connect_rdp(self, info):
        if not winutil.IS_WINDOWS:
            QMessageBox.information(self, APP_NAME,
                                    "RDP доступен только на Windows.")
            return
        # Адрес берём живой (peer_ip) — он не зависит от смены IP машиной.
        ip = info.rdp_ip
        # Учётка локальная на целевой машине → "ИМЯ_ПК\Администратор".
        # Предполагается, что пароль Администратора одинаков на всех ПК.
        user = self.admin_user.split("\\")[-1]
        try:
            winutil.store_rdp_credentials(ip, user, self.admin_password,
                                          domain=info.hostname)
            # Спрашиваем у агента, есть ли сейчас работающий пользователь.
            sessions = self.server.request_sessions(info.agent_id)
            target = winutil.pick_target_session(sessions or [])
            if target:
                # Кто-то за компьютером — подключаемся теневым сеансом,
                # не выкидывая пользователя.
                winutil.shadow_session(ip, target["id"], control=True)
                who = target.get("user") or target.get("station") or \
                    f"сеанс {target['id']}"
                self.append_log(
                    f"Теневое подключение к {info.hostname} ({ip}): "
                    f"{who} [{target['state_name']}]")
            else:
                # За компьютером никого нет — обычный вход на рабочий стол.
                winutil.connect_rdp(ip, user, self.admin_password,
                                    domain=info.hostname)
                self.append_log(
                    f"Подключаюсь к рабочему столу {info.hostname} ({ip})")
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, APP_NAME, f"Ошибка подключения: {exc}")

    # ---- обновление консоли ----------------------------------------------

    def check_updates(self, manual):
        """Проверить обновления в фоне (сеть), результат — через сигналы."""
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Проверяю…")

        def worker():
            try:
                rel = updater.get_latest_release()
            except Exception as exc:  # noqa: BLE001
                self.bridge.update_error.emit(str(exc))
                return
            if rel and updater.is_newer(rel["version"]):
                self.bridge.update_available.emit(rel["version"], rel["notes"])
            else:
                self.bridge.update_absent.emit(manual)

        threading.Thread(target=worker, daemon=True).start()

    def _reset_update_btn(self):
        self.update_btn.setEnabled(True)
        self.update_btn.setText("Проверить обновления")

    def _on_update_available(self, version, notes):
        self._reset_update_btn()
        text = (f"Доступна новая версия {version} (сейчас {VERSION}).\n\n"
                f"{notes[:600] if notes else ''}\n\nОбновить сейчас?")
        if QMessageBox.question(self, APP_NAME, text) != QMessageBox.Yes:
            return
        self.append_log(f"Скачиваю обновление консоли {version}…")
        self.update_btn.setEnabled(False)
        self.update_btn.setText("Обновляю…")

        def worker():
            try:
                res = updater.console_fetch_update(INSTALL_DIR)
            except Exception as exc:  # noqa: BLE001
                self.bridge.update_error.emit(str(exc))
                return
            if not res:
                self.bridge.update_absent.emit(True)
                return
            _ver, _notes, staging = res
            # Команда перезапуска консоли после подмены файлов.
            pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
            runner = pyw if os.path.exists(pyw) else sys.executable
            restart = [runner, os.path.join(INSTALL_DIR, "console.py")]
            updater.apply_console_update(staging, INSTALL_DIR, restart)
            # Закрываемся — помощник дождётся выхода и перезапустит консоль.
            QApplication.quit()

        threading.Thread(target=worker, daemon=True).start()

    def _on_update_absent(self, manual):
        self._reset_update_btn()
        if manual:
            QMessageBox.information(self, APP_NAME,
                                   f"У вас последняя версия ({VERSION}).")

    def _on_update_error(self, msg):
        self._reset_update_btn()
        self.append_log(f"Проверка обновлений не удалась: {msg}")

    # ---- оформление -------------------------------------------------------

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget { background: #0f1420; color: #e6ebf2;
                      font-family: 'Segoe UI', sans-serif; font-size: 13px; }
            QFrame#agentCard { background: #19202e; border: 1px solid #222c3d;
                               border-radius: 10px; }
            QFrame#agentCard:hover { border: 1px solid #3a4a66; }
            QFrame#agentCard QLabel, QFrame#agentCard QCheckBox {
                               background: transparent; }
            QTextEdit#output { background: #0b0f18; border: 1px solid #222c3d;
                               border-radius: 8px; }
            QLineEdit, QComboBox, QSpinBox { background: #19202e;
                               border: 1px solid #2a3547; border-radius: 6px;
                               padding: 6px; }
            QPushButton { background: #223049; border: none; border-radius: 6px;
                          padding: 7px 14px; color: #e6ebf2; }
            QPushButton:hover { background: #2c3e5e; }
            QPushButton#runAll { background: #2f6fed; }
            QPushButton#runAll:hover { background: #3f7ff5; }
            QPushButton#rdpBtn { background: #1f6f4a; }
            QPushButton#rdpBtn:hover { background: #268a5b; }
            QScrollArea { border: none; }
        """)


def _esc(text):
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))


# ---- точка входа ----------------------------------------------------------

def main():
    app = QApplication(sys.argv)

    cfg = load_console_config()
    if not cfg or not cfg.get("token"):
        setup = SetupDialog()
        if setup.exec() != QDialog.Accepted:
            return
        cfg = load_console_config()
        admin_user = setup.admin_user
        admin_password = setup.admin_password
    else:
        login = LoginDialog(cfg.get("admin_user", "Administrator"))
        if login.exec() != QDialog.Accepted:
            return
        admin_user = login.admin_user
        admin_password = login.admin_password

    port = cfg["port"]
    token = cfg["token"]

    bridge = Bridge()
    server = MasterServer(
        port=port, token=token,
        on_change=bridge.agents_changed.emit,
        on_result=lambda aid, res: bridge.result_received.emit(aid, res),
        on_log=bridge.log_line.emit,
    )
    try:
        server.start()
    except OSError as exc:
        QMessageBox.critical(None, APP_NAME,
                             f"Не удалось занять порт {port}: {exc}")
        return

    window = MainWindow(server, bridge, admin_user, admin_password, token, port)

    # Периодически обновляем список (на случай пропущенных событий).
    timer = QTimer()
    timer.timeout.connect(window.refresh_agents)
    timer.start(5000)

    window.show()
    exit_code = app.exec()
    server.stop()
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
