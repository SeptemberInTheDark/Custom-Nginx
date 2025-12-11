import asyncio
from dataclasses import dataclass, field
from typing import List, Tuple, AsyncIterator
from contextlib import asynccontextmanager


@dataclass
class Upstream:
    host: str
    port: int
    max_connections: int = 100
    semaphore: asyncio.Semaphore = field(default=None, repr=False)

    def __post_init__(self):
        if self.semaphore is None:
            self.semaphore = asyncio.Semaphore(self.max_connections)

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


class UpstreamPool:
    """Пул апстримов с round-robin балансировкой и лимитами соединений."""

    def __init__(self, upstreams: List[Upstream]):
        if not upstreams:
            raise ValueError("At least one upstream is required")
        self._upstreams = upstreams
        self._index = 0
        self._lock = asyncio.Lock()

    async def get_next(self) -> Upstream:
        """Round-robin выбор апстрима."""
        async with self._lock:
            upstream = self._upstreams[self._index]
            self._index = (self._index + 1) % len(self._upstreams)
            return upstream

    @asynccontextmanager
    async def acquire_connection(
        self, timeout: float
    ) -> AsyncIterator[Tuple[asyncio.StreamReader, asyncio.StreamWriter, Upstream]]:
        """
        Получить соединение к апстриму с учётом лимита.
        
        Возвращает кортеж (reader, writer, upstream) для логирования.
        """
        upstream = await self.get_next()
        writer = None

        # Ждём слот в семафоре (ограничение соединений к апстриму)
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
                        pass

    @property
    def upstreams(self) -> List[Upstream]:
        return self._upstreams.copy()

    def __len__(self) -> int:
        return len(self._upstreams)
