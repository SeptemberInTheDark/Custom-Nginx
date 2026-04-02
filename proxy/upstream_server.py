# upstream_server.py
"""
Правильный upstream сервер для тестирования прокси.
Поддерживает keep-alive и высокую нагрузку.
"""
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s'
)
logger = logging.getLogger("upstream")

class UpstreamServer:
    """Upstream сервер с поддержкой keep-alive."""
    
    def __init__(self, host: str = "127.0.0.1", port: int = 9001):
        self.host = host
        self.port = port
        self.server = None
        self.connections = 0
        self.total_requests = 0
        
    async def handle_connection(
        self, 
        reader: asyncio.StreamReader, 
        writer: asyncio.StreamWriter
    ) -> None:
        """
        Обрабатывает HTTP запросы с поддержкой keep-alive.
        """
        client_addr = writer.get_extra_info('peername')
        self.connections += 1
        
        try:
            while True:  # keep-alive loop
                # Читаем запрос
                request_line = await reader.readline()
                if not request_line:
                    break
                
                # Парсим метод и путь
                parts = request_line.decode('latin-1').split()
                if len(parts) < 2:
                    break
                
                method = parts[0]
                path = parts[1]
                
                # Читаем заголовки
                headers = {}
                content_length = 0
                connection_close = False
                
                while True:
                    line = await reader.readline()
                    if line in (b'\r\n', b'\n', b''):
                        break
                    if not line:
                        break
                    
                    decoded = line.decode('latin-1').strip()
                    if ':' in decoded:
                        key, value = decoded.split(':', 1)
                        key_lower = key.lower()
                        headers[key_lower] = value.strip()
                        
                        if key_lower == 'content-length':
                            content_length = int(value.strip())
                        elif key_lower == 'connection':
                            if value.strip().lower() == 'close':
                                connection_close = True
                
                # Читаем тело если есть
                body = b""
                if content_length > 0:
                    body = await reader.readexactly(content_length)
                
                # Формируем ответ
                self.total_requests += 1
                response_body = f"OK from upstream {self.port} - request #{self.total_requests}".encode()
                
                response_lines = [
                    f"HTTP/1.1 200 OK".encode(),
                    b"Content-Type: text/plain",
                    f"Content-Length: {len(response_body)}".encode(),
                    b"Connection: keep-alive" if not connection_close else b"Connection: close",
                    b"",
                    b""
                ]
                
                # Отправляем ответ
                writer.write(b"\r\n".join(response_lines))
                writer.write(response_body)
                await writer.drain()
                
                logger.debug(f"[{self.port}] {method} {path} -> 200 (conn: {self.connections}, total: {self.total_requests})")
                
                # Если клиент просит закрыть соединение - выходим
                if connection_close:
                    break
                    
        except asyncio.IncompleteReadError:
            # Клиент закрыл соединение - нормально для keep-alive
            pass
        except Exception as e:
            logger.error(f"Error handling connection: {e}")
        finally:
            self.connections -= 1
            writer.close()
            await writer.wait_closed()
            logger.debug(f"[{self.port}] Connection closed (active: {self.connections})")
    
    async def start(self):
        """Запускает upstream сервер."""
        self.server = await asyncio.start_server(
            self.handle_connection,
            self.host,
            self.port,
            backlog=8192,
            reuse_address=True,
            limit=131072
        )
        
        logger.info(f" Upstream server running on {self.host}:{self.port}")

        logger.info(f"   Keep-alive: ENABLED")
        
        async with self.server:
            await self.server.serve_forever()
    
    async def stop(self):
        """Останавливает сервер."""
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            logger.info(f"Upstream {self.port} stopped")

async def main():
    """Запускает два upstream сервера."""
    upstream1 = UpstreamServer(port=9001)
    upstream2 = UpstreamServer(port=9002)
    
    # Запускаем оба сервера конкурентно
    await asyncio.gather(
        upstream1.start(),
        upstream2.start()
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutting down...")