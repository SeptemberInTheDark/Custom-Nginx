"""
TCP-сервер для приёма клиентских соединений.

Использует asyncio.start_server() — низкоуровневый, но простой API.
Каждое соединение обрабатывается в отдельной корутине.
"""
import asyncio
import logging
from typing import Optional

from proxy.client_handler import handle_client
from proxy.config import ProxyConfig
from proxy.upstream_pool import UpstreamPool, Upstream
from proxy.logger import generate_trace_id, set_trace_id

logger = logging.getLogger("proxy")


class ProxyServer:
    """
    Основной класс сервера.
    
    Принимает TCP-соединения, ограничивает их количество через семафор
    и делегирует обработку в handle_client().
    """

    def __init__(self, config: ProxyConfig):
        self.config = config
        self.upstream_pool = self._create_upstream_pool()
        # семафор для ограничения одновременных клиентов
        self._client_semaphore = asyncio.Semaphore(config.limits.max_client_conns)
        self._server: Optional[asyncio.Server] = None
        self._active_connections = 0

    def _create_upstream_pool(self) -> UpstreamPool:
        """Конвертируем конфиг в рабочие Upstream-объекты с семафорами."""
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
        """
        Запуск сервера.
        
        start_server() создаёт сокет и начинает принимать соединения.
        Для каждого нового соединения вызывается _handle_client_wrapper.
        """
        self._server = await asyncio.start_server(
            self._handle_client_wrapper,
            self.config.listen_host,
            self.config.listen_port,
        )

        addr = f"{self.config.listen_host}:{self.config.listen_port}"
        logger.info(f"Proxy server started on {addr}")
        logger.info(f"Upstreams: {[u.address for u in self.upstream_pool.upstreams]}")

        # serve_forever() блокирует до вызова close()
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Корректная остановка — ждём закрытия всех соединений."""
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
        """
        Обёртка над handle_client с проверкой лимита.
        
        Если семафор locked() — все слоты заняты, сразу отдаём 503.
        Это лучше чем вешать клиента в очередь на неопределённое время.
        """
        # быстрая проверка без блокировки
        if self._client_semaphore.locked():
            # даже для отклонённых запросов генерируем trace_id
            trace_id = generate_trace_id()
            set_trace_id(trace_id)
            
            client_addr = writer.get_extra_info("peername")
            logger.warning(f"Connection rejected from {client_addr}: limit exceeded")
            try:
                response = (
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Content-Length: 0\r\n"
                    b"Connection: close\r\n"
                    b"X-Trace-Id: " + trace_id.encode() + b"\r\n"
                    b"\r\n"
                )
                writer.write(response)
                await writer.drain()
            except Exception:
                pass
            finally:
                writer.close()
                await writer.wait_closed()
            return

        # нормальная обработка — захватываем слот в семафоре
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
        """Для метрик и отладки."""
        return self._active_connections
