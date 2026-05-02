"""
Пул upstream-серверов с балансировкой.

Реализует:
- round-robin выбор upstream
- ограничение соединений к каждому upstream через семафор
- keep-alive reuse upstream-соединений
- автоматическое закрытие сломанных соединений через контекстный менеджер
"""

import asyncio
import logging
import socket
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import AsyncIterator, Deque, Dict, List
from contextlib import asynccontextmanager

logger = logging.getLogger("proxy")


@dataclass
class Upstream:
    """
    Один upstream-сервер.

    Семафор создаётся в __post_init__ — это хак,
    потому что asyncio.Semaphore нельзя создать в default_factory
    (нужен запущенный event loop).
    """

    host: str
    port: int
    max_connections: int = 200
    semaphore: asyncio.Semaphore = field(default=None, repr=False)

    def __post_init__(self):
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.max_connections)

    @property
    def address(self) -> str:
        """Для логов и метрик."""
        return f"{self.host}:{self.port}"


@dataclass
class UpstreamConnection:
    """Открытое соединение к upstream.

    `reusable` выставляет client_handler после чтения ответа. Если ответ был
    close-delimited или upstream попросил `Connection: close`, сокет нельзя
    возвращать в пул: следующий HTTP-ответ будет невозможно отделить.
    """

    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    upstream: Upstream
    reusable: bool = True


class UpstreamPool:
    """
    Пул с round-robin балансировкой.

    Round-robin простой, но работает неплохо когда upstreams примерно
    одинаковые по производительности. Для разных весов нужен weighted RR.
    """

    def __init__(self, upstreams: List[Upstream]):
        if not upstreams:
            raise ValueError("At least one upstream is required")
        self._upstreams = upstreams
        self._index = 0
        self._rr_lock = asyncio.Lock()
        self._idle: Dict[str, Deque[UpstreamConnection]] = defaultdict(deque)

    async def get_next(self) -> Upstream:
        """
        Выбирает следующий upstream по кругу.

        Лок нужен, чтобы несколько задач не выбирали один и тот же индекс одновременно.
        """
        async with self._rr_lock:
            start = self._index
            self._index = (self._index + 1) % len(self._upstreams)

            # Сохраняем round-robin порядок, но не заставляем запрос ждать
            # занятый upstream, если следующий прямо сейчас свободен.
            for offset in range(len(self._upstreams)):
                candidate = self._upstreams[(start + offset) % len(self._upstreams)]
                if self._idle[candidate.address] or not candidate.semaphore.locked():
                    return candidate

            return self._upstreams[start]

    @asynccontextmanager
    async def acquire_connection(
        self, timeout: float
    ) -> AsyncIterator[UpstreamConnection]:
        """
        Получает соединение к upstream.

        1. Выбираем upstream (round-robin)
        2. Ждём слот в семафоре (лимит соединений)
        3. Переиспользуем idle keep-alive соединение или открываем TCP-соединение
        4. yield - отдаём наружу
        5. finally - возвращаем живой сокет в пул или закрываем

        Возвращаем и upstream чтобы знать куда попали (для логов).
        """
        upstream = await self.get_next()
        conn = None
        acquired = False

        try:
            conn = self._take_idle(upstream)

            if conn is None:
                await asyncio.wait_for(upstream.semaphore.acquire(), timeout=timeout)
                acquired = True
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(upstream.host, upstream.port),
                    timeout=timeout,
                )

                # Оптимизируем сокет - отключаем Nagle алгоритм
                sock = writer.get_extra_info("socket")
                if sock:
                    try:
                        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    except (AttributeError, OSError):
                        pass

                conn = UpstreamConnection(reader, writer, upstream)

            try:
                yield conn
            except BaseException:
                conn.reusable = False
                raise
        finally:
            if conn is None:
                if acquired:
                    upstream.semaphore.release()
                return

            if conn.reusable and not conn.writer.is_closing():
                self._idle[conn.upstream.address].append(conn)
            else:
                await self._close_connection(conn)
                upstream.semaphore.release()

    @property
    def upstreams(self) -> List[Upstream]:
        """Копия списка для безопасности."""
        return self._upstreams.copy()

    def __len__(self) -> int:
        return len(self._upstreams)

    def _take_idle(self, upstream: Upstream) -> UpstreamConnection | None:
        """Достаёт живое keep-alive соединение из пула."""
        idle = self._idle[upstream.address]
        while idle:
            conn = idle.pop()
            if not conn.writer.is_closing():
                conn.reusable = True
                return conn
            upstream.semaphore.release()
        return None

    async def _close_connection(self, conn: UpstreamConnection) -> None:
        """Закрывает upstream-соединение без проброса ошибок наружу."""
        try:
            conn.writer.close()
            await conn.writer.wait_closed()
        except Exception:
            pass  # уже закрыт или сломался
