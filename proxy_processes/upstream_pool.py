"""
Пул upstream-серверов для одного worker-процесса.

Между процессами не шарится — у каждого worker'а свой пул.
Это означает, что суммарное число коннектов к upstream =
workers * max_conns_per_upstream. Уменьшайте лимит, если воркеров много.

Внутри одного процесса — thread-safe: семафор + lock на idle deque.
Семантика семафора:
  acquire — при создании нового сокета;
  release — при его реальном закрытии;
  переходы idle <-> in-use семафор не трогают.
"""
import socket
import threading
from collections import deque
from typing import List, Tuple

from .config import UpstreamConfig


class UpstreamPool:
    def __init__(self, upstreams: List[UpstreamConfig], max_per_upstream: int):
        if not upstreams:
            raise ValueError("At least one upstream is required")
        self._ups = list(upstreams)
        self._idx = 0
        self._rr_lock = threading.Lock()
        self._max = max_per_upstream
        self._slots = {u.address: threading.BoundedSemaphore(max_per_upstream) for u in self._ups}
        self._idle: dict = {u.address: deque() for u in self._ups}
        self._idle_lock = threading.Lock()
        self._addr_to_hostport: dict = {u.address: (u.host, u.port) for u in self._ups}

    def acquire(self, timeout: float) -> Tuple[socket.socket, str]:
        """
        Возвращает (sock, addr) — готовый к использованию сокет.
        Сначала пробует idle keep-alive; иначе ждёт слот семафора и
        открывает новое TCP-соединение.

        Бросает TimeoutError или OSError при провале.
        """
        u = self._next()
        addr = u.address

        # 1) idle keep-alive
        with self._idle_lock:
            q = self._idle[addr]
            if q:
                sock = q.popleft()
                # сбрасываем таймаут на случай, если был выставлен другой
                try:
                    sock.settimeout(timeout)
                except OSError:
                    pass
                return sock, addr

        # 2) acquire slot — блокирующий с таймаутом
        if not self._slots[addr].acquire(timeout=timeout):
            raise TimeoutError(f"upstream {addr} pool exhausted")

        # 3) открываем новое TCP-соединение
        try:
            host, port = self._addr_to_hostport[addr]
            sock = socket.create_connection((host, port), timeout=timeout)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return sock, addr
        except Exception:
            # на ошибке коннекта — возвращаем слот в семафор
            try:
                self._slots[addr].release()
            except ValueError:
                pass
            raise

    def release(self, addr: str, sock: socket.socket, reusable: bool) -> None:
        """
        reusable=True — кладём сокет обратно в idle (слот семафора занят).
        reusable=False — закрываем сокет и освобождаем слот.
        """
        if reusable and sock is not None:
            try:
                with self._idle_lock:
                    self._idle[addr].append(sock)
                return
            except Exception:
                pass
        # закрываем
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        try:
            self._slots[addr].release()
        except ValueError:
            pass

    def close_all_idle(self) -> None:
        """Используется при graceful shutdown воркера."""
        with self._idle_lock:
            for addr, q in self._idle.items():
                while q:
                    sock = q.popleft()
                    try:
                        sock.close()
                    except Exception:
                        pass
                    try:
                        self._slots[addr].release()
                    except ValueError:
                        pass

    def _next(self) -> UpstreamConfig:
        with self._rr_lock:
            u = self._ups[self._idx % len(self._ups)]
            self._idx += 1
            return u
