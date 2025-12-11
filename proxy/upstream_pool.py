"""
Пул upstream-серверов с балансировкой.

Реализует:
- round-robin выбор upstream
- ограничение соединений к каждому upstream через семафор
- автоматическое закрытие соединений через контекстный менеджер
"""
import asyncio
from dataclasses import dataclass, field
from typing import List, Tuple, AsyncIterator
from contextlib import asynccontextmanager


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
    max_connections: int = 100
    semaphore: asyncio.Semaphore = field(default=None, repr=False)

    def __post_init__(self):
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.max_connections)

    @property
    def address(self) -> str:
        """Для логов и метрик."""
        return f"{self.host}:{self.port}"


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
        # lock нужен чтобы _index не сломался при конкурентных запросах
        self._lock = asyncio.Lock()

    async def get_next(self) -> Upstream:
        """
        Выбирает следующий upstream по кругу.
        
        Lock здесь дешёвый — просто инкремент под защитой.
        """
        async with self._lock:
            upstream = self._upstreams[self._index]
            self._index = (self._index + 1) % len(self._upstreams)
            return upstream

    @asynccontextmanager
    async def acquire_connection(
        self, timeout: float
    ) -> AsyncIterator[Tuple[asyncio.StreamReader, asyncio.StreamWriter, Upstream]]:
        """
        Получает соединение к upstream.
        
        1. Выбираем upstream (round-robin)
        2. Ждём слот в семафоре (лимит соединений)
        3. Открываем TCP-соединение
        4. yield — отдаём наружу
        5. finally — гарантированно закрываем
        
        Возвращаем и upstream чтобы знать куда попали (для логов).
        """
        upstream = await self.get_next()
        writer = None

        async with upstream.semaphore:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(upstream.host, upstream.port),
                    timeout=timeout
                )
                yield reader, writer, upstream
            finally:
                if writer is not None:
                    try:
                        writer.close()
                        await writer.wait_closed()
                    except Exception:
                        pass  # уже закрыт или сломался — ок

    @property
    def upstreams(self) -> List[Upstream]:
        """Копия списка для безопасности."""
        return self._upstreams.copy()

    def __len__(self) -> int:
        return len(self._upstreams)
