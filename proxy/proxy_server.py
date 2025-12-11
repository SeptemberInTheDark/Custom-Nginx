import asyncio
import logging
import signal
from typing import Optional

from proxy.client_handler import handle_client
from proxy.config import ProxyConfig
from proxy.upstream_pool import UpstreamPool, Upstream

logger = logging.getLogger("proxy")


class ProxyServer:
    """Асинхронный reverse proxy сервер."""

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.upstream_pool = self._create_upstream_pool()
        self._client_semaphore = asyncio.Semaphore(config.limits.max_client_conns)
        self._server: Optional[asyncio.Server] = None
        self._active_connections = 0

    def _create_upstream_pool(self) -> UpstreamPool:
        """Создаёт пул апстримов из конфигурации."""
        upstreams = [
            Upstream(
                host=u.host,
                port=u.port,
                max_connections=self.config.limits.max_conns_per_upstream,
            )
            for u in self.config.upstreams
        ]
        return UpstreamPool(upstreams)

    async def start(self) -> None:
        """Запуск сервера."""
        self._server = await asyncio.start_server(
            self._handle_client_wrapper,
            self.config.listen_host,
            self.config.listen_port,
        )

        addr = f"{self.config.listen_host}:{self.config.listen_port}"
        logger.info(f"Proxy server started on {addr}")
        logger.info(f"Upstreams: {[u.address for u in self.upstream_pool.upstreams]}")

        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Остановка сервера."""
        if self._server:
            logger.info("Stopping proxy server...")
            self._server.close()
            await self._server.wait_closed()
            logger.info("Proxy server stopped")

    async def _handle_client_wrapper(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Обёртка для обработки клиента с учётом лимита соединений."""
        # Проверяем лимит соединений без блокировки
        if self._client_semaphore.locked():
            # Превышен лимит — отклоняем соединение
            client_addr = writer.get_extra_info("peername")
            logger.warning(f"[{client_addr}] Connection rejected: limit exceeded")
            try:
                writer.write(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
            return

        async with self._client_semaphore:
            self._active_connections += 1
            try:
                await handle_client(
                    reader,
                    writer,
                    self.upstream_pool,
                    self.config.timeouts,
                )
            finally:
                self._active_connections -= 1

    @property
    def active_connections(self) -> int:
        return self._active_connections
