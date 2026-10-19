"""
protocol.py — общий протокол обмена между мастер-консолью и агентами.

Транспорт: обычный TCP.
Кадрирование: 4 байта длины (big-endian, без знака) + тело в виде UTF-8 JSON.
Аутентификация: агент при подключении присылает общий токен (см. тип "hello").

Файл не зависит от ОС и используется и мастером, и агентом.
"""

import json
import struct

# Версия протокола — на случай будущих изменений формата.
PROTOCOL_VERSION = 1

# Максимальный размер одного сообщения (защита от мусора/переполнения).
MAX_MESSAGE_BYTES = 50 * 1024 * 1024  # 50 МБ

# ---- Типы сообщений -------------------------------------------------------

# агент -> мастер
MSG_HELLO = "hello"            # первичная регистрация: токен + сведения о машине
MSG_HEARTBEAT = "heartbeat"    # периодический сигнал "я жив"
MSG_RESULT = "result"          # результат выполнения команды
MSG_PONG = "pong"              # ответ на ping
MSG_SESSIONS = "sessions"      # список сеансов пользователей (ответ)

# мастер -> агент
MSG_HELLO_OK = "hello_ok"      # регистрация принята
MSG_ERROR = "error"            # ошибка (например, неверный токен)
MSG_RUN = "run"                # выполнить команду
MSG_PING = "ping"              # проверка связи
MSG_GET_SESSIONS = "get_sessions"  # запросить список сеансов (для теневого RDP)


class ProtocolError(Exception):
    """Нарушение формата протокола или обрыв соединения при чтении."""


def _recv_exact(sock, n):
    """Прочитать ровно n байт из сокета или бросить ProtocolError при обрыве."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ProtocolError("соединение закрыто удалённой стороной")
        buf.extend(chunk)
    return bytes(buf)


def send_msg(sock, obj):
    """Отправить словарь obj как одно сообщение протокола."""
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(data) > MAX_MESSAGE_BYTES:
        raise ProtocolError("сообщение превышает допустимый размер")
    header = struct.pack(">I", len(data))
    sock.sendall(header + data)


def recv_msg(sock):
    """Прочитать одно сообщение протокола и вернуть его как словарь."""
    header = _recv_exact(sock, 4)
    (length,) = struct.unpack(">I", header)
    if length > MAX_MESSAGE_BYTES:
        raise ProtocolError("заявленный размер сообщения слишком велик")
    data = _recv_exact(sock, length)
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError(f"некорректный JSON: {exc}") from exc
