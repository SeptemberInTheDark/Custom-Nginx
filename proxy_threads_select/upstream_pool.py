"""
Thread-safe пул upstream-серверов.

Шарится между всеми reactor-потоками. Хранит:
- список UpstreamConfig + round-robin индекс под Lock;
- BoundedSemaphore на каждый upstream — лимит "live" соединений
  (in-use + idle), чтобы не завалить бэкенд;
- deque idle-сокетов на каждый upstream для keep-alive reuse.

Семантика семафора: acquire при создании нового сокета, release
при его закрытии. Idle <-> in-use переходы семафор не трогают
(счётчик отражает "сколько коннектов мы держим живыми").
"""
import socket
import threading
from collections import deque
from typing import List, Optional, Tuple

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

    @property
    def upstreams(self) -> List[UpstreamConfig]:
        return list(self._ups)

    def pick_upstream(self) -> UpstreamConfig:
        """Round-robin выбор upstream."""
        with self._rr_lock:
            u = self._ups[self._idx % len(self._ups)]
            self._idx += 1
            return u

    def take_idle(self, addr: str) -> Optional[socket.socket]:
        """Достаёт keep-alive сокет из idle-пула либо None."""
        with self._idle_lock:
            q = self._idle[addr]
            while q:
                sock = q.popleft()
                # is_closing на blocking-сокете не проверишь дёшево;
                # сломанный сокет даст ошибку при первом write — мы это переживём.
                return sock
        return None

    def acquire_slot(self, addr: str) -> bool:
        """Резервирует слот под новый коннект. Non-blocking."""
        sema = self._slots.get(addr)
        if sema is None:
            return False
        return sema.acquire(blocking=False)

    def release_slot(self, addr: str) -> None:
        sema = self._slots.get(addr)
        if sema is None:
            return
        try:
            sema.release()
        except ValueError:
            pass

    def put_idle(self, addr: str, sock: socket.socket) -> None:
        """Возвращает живой keep-alive сокет в pool. Слот семафора НЕ трогаем."""
        with self._idle_lock:
            self._idle[addr].append(sock)

    def host_port(self, addr: str) -> Tuple[str, int]:
        return self._addr_to_hostport[addr]
