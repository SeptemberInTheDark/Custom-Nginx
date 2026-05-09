"""
Reactor: отдельный поток с собственным selectors.DefaultSelector.

Каждый reactor обслуживает закреплённые за ним клиентские сессии.
Acceptor закидывает новых клиентов в очередь reactor.new_clients и
будит select() через socketpair, чтобы тот сразу подхватил.

Внутри одного reactor'а работа кооперативная (без локов на сессиях),
поэтому Session.* можно дёргать без синхронизации между event'ами.
"""
import queue
import selectors
import socket
import threading
import time

from .conn import Session, S
from .logger import get_logger

log = get_logger()


class Reactor(threading.Thread):
    def __init__(self, idx: int, pool, timeouts):
        super().__init__(daemon=True, name=f"reactor-{idx}")
        self.idx = idx
        self.pool = pool
        self.timeouts = timeouts
        self.selector = selectors.DefaultSelector()
        self.new_clients: "queue.Queue[tuple]" = queue.Queue()
        self._sessions: list = []
        self._stopping = False

        # wakeup-pair: пишем туда, чтобы прервать select() в этом потоке
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._wake_w.setblocking(False)
        self.selector.register(self._wake_r, selectors.EVENT_READ, self._on_wake)

    # ── api для acceptor ─────────────────────────────────────────────

    def push_client(self, sock: socket.socket, addr) -> None:
        self.new_clients.put((sock, addr))
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def stop(self) -> None:
        self._stopping = True
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    # ── api для Session ──────────────────────────────────────────────

    def register(self, sock, events, callback) -> None:
        try:
            self.selector.register(sock, events, callback)
        except KeyError:
            # уже зарегистрирован — модифицируем
            self.selector.modify(sock, events, callback)

    def modify(self, sock, events, callback) -> None:
        try:
            self.selector.modify(sock, events, callback)
        except KeyError:
            self.selector.register(sock, events, callback)

    def unregister(self, sock) -> None:
        try:
            self.selector.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass

    # ── главный цикл ─────────────────────────────────────────────────

    def run(self) -> None:
        log.info(f"reactor #{self.idx} started")
        while not self._stopping:
            timeout = self._next_timeout()
            try:
                events = self.selector.select(timeout=timeout)
            except OSError:
                events = []

            for key, mask in events:
                cb = key.data
                try:
                    cb(mask)
                except Exception as e:
                    log.exception(f"callback error in reactor #{self.idx}: {e}")

            now = time.monotonic()
            self._check_timeouts(now)
            # подметаем закрытые сессии
            if self._sessions:
                self._sessions = [s for s in self._sessions if s.state != S.CLOSED]

        # shutdown
        for s in list(self._sessions):
            try:
                s.close()
            except Exception:
                pass
        try:
            self.selector.close()
        except Exception:
            pass
        try:
            self._wake_r.close()
            self._wake_w.close()
        except Exception:
            pass
        log.info(f"reactor #{self.idx} stopped")

    # ── helpers ──────────────────────────────────────────────────────

    def _next_timeout(self) -> float:
        """Время до ближайшего дедлайна сессии. Не больше 1.0 секунды."""
        timeout = 1.0
        now = time.monotonic()
        for s in self._sessions:
            if s.state == S.CLOSED:
                continue
            rem = s.deadline - now
            if rem < 0:
                return 0.0
            if rem < timeout:
                timeout = rem
        return max(0.0, timeout)

    def _check_timeouts(self, now: float) -> None:
        for s in self._sessions:
            if s.state == S.CLOSED:
                continue
            if now < s.deadline:
                continue
            log.warning(f"[{s.trace_id}] timeout in state={s.state}")
            if s.state == S.READ_REQ_HEADERS and s.req_count == 0:
                # idle на старте — просто закрываем
                s.close()
            elif s.state == S.READ_REQ_HEADERS and s.req_count > 0:
                # keep-alive idle timeout
                s.close()
            else:
                try:
                    s._send_error_and_close(504, "Gateway Timeout")
                except Exception:
                    s.close()

    def _on_wake(self, _mask) -> None:
        # прочитать всё что есть в pipe
        try:
            while True:
                if not self._wake_r.recv(4096):
                    break
        except BlockingIOError:
            pass
        # вытащить всех новых клиентов
        while True:
            try:
                sock, addr = self.new_clients.get_nowait()
            except queue.Empty:
                break
            try:
                sock.setblocking(False)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            sess = Session(sock, addr, self, self.pool, self.timeouts)
            self._sessions.append(sess)
            try:
                sess.register_client_read()
            except Exception as e:
                log.warning(f"failed to register client: {e}")
                sess.close()
