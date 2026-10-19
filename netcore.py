"""
netcore.py — сетевое ядро мастер-консоли.

MasterServer слушает TCP-порт, принимает подключения агентов, ведёт список
онлайн-машин, рассылает команды (одной / выбранным / всем) и собирает
результаты. Код не зависит от GUI: события отдаются через колбэки, которые
GUI оборачивает в сигналы Qt.

Потоки:
  * один поток принимает новые подключения (accept);
  * на каждого агента — свой поток-читатель;
  * один поток-монитор помечает «молчащих» агентов офлайн.
Запись в сокет защищена отдельным замком на каждого агента.
"""

import hmac
import threading
import time
import uuid
from dataclasses import dataclass, field

import protocol


@dataclass
class AgentInfo:
    """Публичные сведения об агенте — то, что видит консоль."""
    agent_id: str
    hostname: str
    username: str
    os: str
    ip: str              # адрес, как его сообщил сам агент
    connected_at: float
    last_seen: float
    online: bool = True
    peer_ip: str = ""    # адрес, с которого реально пришло подключение (всегда актуальный)
    version: str = ""    # версия агента

    @property
    def rdp_ip(self):
        """Адрес для подключения к рабочему столу — всегда живой peer_ip."""
        return self.peer_ip or self.ip


@dataclass
class _AgentConn:
    """Внутреннее состояние подключения: инфо + сокет + замок на запись."""
    info: AgentInfo
    sock: object
    write_lock: threading.Lock = field(default_factory=threading.Lock)


# Через столько секунд молчания агент считается отключившимся.
HEARTBEAT_TIMEOUT = 30.0


class MasterServer:
    def __init__(self, port, token,
                 on_change=None, on_result=None, on_log=None):
        self.port = int(port)
        self.token = token
        self.on_change = on_change or (lambda: None)
        self.on_result = on_result or (lambda agent_id, result: None)
        self.on_log = on_log or (lambda line: None)

        self._agents = {}                 # agent_id -> _AgentConn
        self._lock = threading.RLock()    # защищает _agents
        self._server_sock = None
        self._running = False
        self._threads = []
        # Ожидающие ответы со списком сеансов: agent_id -> (Event, [holder]).
        self._session_waiters = {}
        self._waiters_lock = threading.Lock()

    # ---- жизненный цикл ---------------------------------------------------

    def start(self):
        import socket
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("0.0.0.0", self.port))
        self._server_sock.listen(64)
        self._running = True

        self._spawn(self._accept_loop, "accept")
        self._spawn(self._monitor_loop, "monitor")
        self.on_log(f"Сервер запущен, слушаю порт {self.port}")

    def stop(self):
        self._running = False
        try:
            if self._server_sock:
                self._server_sock.close()
        except OSError:
            pass
        with self._lock:
            conns = list(self._agents.values())
            self._agents.clear()
        for c in conns:
            try:
                c.sock.close()
            except OSError:
                pass
        self.on_change()

    def _spawn(self, target, name):
        t = threading.Thread(target=target, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    # ---- приём подключений ------------------------------------------------

    def _accept_loop(self):
        while self._running:
            try:
                sock, addr = self._server_sock.accept()
            except OSError:
                break  # сокет закрыт в stop()
            self._spawn(lambda s=sock, a=addr: self._handle_client(s, a),
                        "agent-reader")

    def _handle_client(self, sock, addr):
        peer_ip = addr[0]
        agent_id = None
        try:
            # 1) Рукопожатие: первое сообщение обязано быть hello с верным токеном.
            hello = protocol.recv_msg(sock)
            if hello.get("type") != protocol.MSG_HELLO:
                protocol.send_msg(sock, {"type": protocol.MSG_ERROR,
                                         "msg": "ожидался hello"})
                return
            # Сравнение за постоянное время — не даём подбирать токен по таймингу.
            if not hmac.compare_digest(str(hello.get("token", "")), self.token):
                protocol.send_msg(sock, {"type": protocol.MSG_ERROR,
                                         "msg": "неверный токен"})
                self.on_log(f"Отклонено подключение с {peer_ip}: неверный токен")
                return

            agent_id = hello.get("agent_id") or uuid.uuid4().hex
            now = time.time()
            info = AgentInfo(
                agent_id=agent_id,
                hostname=hello.get("hostname", "?"),
                username=hello.get("username", "?"),
                os=hello.get("os", "?"),
                ip=hello.get("ip") or peer_ip,
                connected_at=now,
                last_seen=now,
                peer_ip=peer_ip,
                version=hello.get("version", ""),
            )
            with self._lock:
                # Если такой id уже был (переподключение) — закрываем старый.
                old = self._agents.get(agent_id)
                if old:
                    try:
                        old.sock.close()
                    except OSError:
                        pass
                self._agents[agent_id] = _AgentConn(info=info, sock=sock)

            protocol.send_msg(sock, {"type": protocol.MSG_HELLO_OK})
            self.on_log(f"Агент онлайн: {info.hostname} ({info.ip})")
            self.on_change()

            # 2) Основной цикл чтения.
            while self._running:
                msg = protocol.recv_msg(sock)
                self._touch(agent_id)
                mtype = msg.get("type")
                if mtype in (protocol.MSG_HEARTBEAT, protocol.MSG_PONG):
                    continue
                if mtype == protocol.MSG_RESULT:
                    self.on_result(agent_id, msg)
                elif mtype == protocol.MSG_SESSIONS:
                    self._fulfil_sessions(agent_id, msg.get("sessions", []))
                # прочие типы молча игнорируем
        except protocol.ProtocolError:
            pass
        except OSError:
            pass
        finally:
            if agent_id:
                self._drop(agent_id, sock)

    # ---- учёт состояния ---------------------------------------------------

    def _touch(self, agent_id):
        with self._lock:
            c = self._agents.get(agent_id)
            if c:
                c.info.last_seen = time.time()
                if not c.info.online:
                    c.info.online = True
                    self.on_change()

    def _drop(self, agent_id, sock):
        changed = False
        with self._lock:
            c = self._agents.get(agent_id)
            if c and c.sock is sock:
                del self._agents[agent_id]
                changed = True
        try:
            sock.close()
        except OSError:
            pass
        if changed:
            self.on_log(f"Агент отключился: {agent_id}")
            self.on_change()

    def _monitor_loop(self):
        while self._running:
            time.sleep(5)
            now = time.time()
            stale = []
            with self._lock:
                for aid, c in self._agents.items():
                    if now - c.info.last_seen > HEARTBEAT_TIMEOUT:
                        stale.append((aid, c.sock))
            for aid, sock in stale:
                # Рвём сокет — поток-читатель проснётся и вызовет _drop().
                try:
                    sock.shutdown(2)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

    # ---- запрос списка сеансов (для теневого RDP) ------------------------

    def _fulfil_sessions(self, agent_id, sessions):
        with self._waiters_lock:
            waiter = self._session_waiters.get(agent_id)
        if waiter:
            event, holder = waiter
            holder.append(sessions)
            event.set()

    def request_sessions(self, agent_id, timeout=6.0):
        """
        Запросить у агента список сеансов пользователей и дождаться ответа.
        Возвращает список словарей [{id, user, state, active}] или None.
        """
        with self._lock:
            conn = self._agents.get(agent_id)
        if not conn:
            return None

        event = threading.Event()
        holder = []
        with self._waiters_lock:
            self._session_waiters[agent_id] = (event, holder)
        try:
            with conn.write_lock:
                protocol.send_msg(conn.sock,
                                  {"type": protocol.MSG_GET_SESSIONS})
        except (OSError, protocol.ProtocolError):
            with self._waiters_lock:
                self._session_waiters.pop(agent_id, None)
            return None

        ok = event.wait(timeout)
        with self._waiters_lock:
            self._session_waiters.pop(agent_id, None)
        return holder[0] if ok and holder else None

    # ---- публичный API для GUI -------------------------------------------

    def list_agents(self):
        """Снимок списка агентов (копии AgentInfo), отсортированный по имени."""
        with self._lock:
            items = [AgentInfo(**vars(c.info)) for c in self._agents.values()]
        items.sort(key=lambda a: a.hostname.lower())
        return items

    def run_command(self, agent_ids, command, shell="auto"):
        """
        Отправить команду выбранным агентам.

        agent_ids — список id (или спецзначение None = всем онлайн).
        Результаты придут асинхронно в on_result(agent_id, result).
        Возвращает batch_id и список id, которым реально отправили.
        """
        batch_id = uuid.uuid4().hex
        with self._lock:
            if agent_ids is None:
                targets = list(self._agents.items())
            else:
                targets = [(aid, self._agents[aid]) for aid in agent_ids
                           if aid in self._agents]

        sent = []
        payload = {"type": protocol.MSG_RUN, "cmd_id": batch_id,
                   "shell": shell, "command": command}
        for aid, conn in targets:
            try:
                with conn.write_lock:
                    protocol.send_msg(conn.sock, payload)
                sent.append(aid)
            except (OSError, protocol.ProtocolError):
                self._drop(aid, conn.sock)
        self.on_log(f"Команда отправлена на {len(sent)} маш.: {command!r}")
        return batch_id, sent
