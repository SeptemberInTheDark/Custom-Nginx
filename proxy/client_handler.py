"""
Обработка клиентских запросов.

Это главная логика проксирования:
- парсим HTTP-запрос
- коннектимся к upstream
- стримим данные туда-сюда
- обрабатываем ошибки
"""
import asyncio
import logging

from proxy.config import TimeoutConfig
from proxy.upstream_pool import UpstreamPool, Upstream
from proxy.timeouts import with_timeout
from proxy.utils.http import HttpRequest, parse_request

logger = logging.getLogger("proxy")

# 16KB — хороший баланс между latency и throughput
# меньше — больше syscall'ов, больше — дольше ждём первый чанк
CHUNK_SIZE = 16 * 1024


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_pool: UpstreamPool,
    timeouts: TimeoutConfig,
) -> None:
    """
    Обрабатывает один HTTP-запрос.
    
    Поток данных:
    client -> parse headers -> upstream -> stream body -> upstream
    upstream -> stream response -> client
    """
    client_addr = client_writer.get_extra_info("peername")
    upstream_info = "unknown"

    try:
        # парсим только заголовки, тело будем стримить
        request = await with_timeout(
            parse_request(client_reader),
            timeouts.read,
            "parsing request"
        )

        logger.debug(f"[{client_addr}] {request.method} {request.path}")

        # берём соединение из пула (с учётом лимитов и round-robin)
        async with upstream_pool.acquire_connection(timeouts.connect) as (up_reader, up_writer, upstream):
            upstream_info = upstream.address
            logger.debug(f"[{client_addr}] -> upstream {upstream_info}")

            # отправляем заголовки запроса
            await forward_request_headers(request, up_writer, timeouts)

            # стримим тело запроса если есть
            if request.content_length:
                await stream_body_fixed(
                    client_reader, up_writer,
                    request.content_length,
                    timeouts
                )
            elif request.is_chunked:
                await stream_body_chunked(client_reader, up_writer, timeouts)

            await up_writer.drain()

            # получаем и стримим ответ
            status_code = await stream_response(up_reader, client_writer, timeouts)
            logger.info(f"[{client_addr}] {request.method} {request.path} -> {upstream_info} | {status_code}")

    except TimeoutError as e:
        logger.warning(f"[{client_addr}] Timeout: {e}")
        await send_error(client_writer, 504, "Gateway Timeout")
    except ConnectionError as e:
        logger.warning(f"[{client_addr}] Connection error: {e}")
        await send_error(client_writer, 502, "Bad Gateway")
    except Exception as e:
        logger.exception(f"[{client_addr}] Unexpected error: {e}")
        await send_error(client_writer, 500, "Internal Server Error")
    finally:
        # всегда закрываем соединение с клиентом
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass


async def forward_request_headers(
    request: HttpRequest,
    writer: asyncio.StreamWriter,
    timeouts: TimeoutConfig,
) -> None:
    """
    Пересылает HTTP-заголовки upstream'у.
    
    Формат HTTP/1.1:
    GET /path HTTP/1.1\r\n
    Host: example.com\r\n
    \r\n
    """
    # request line
    start_line = f"{request.method} {request.path} {request.version}\r\n"
    writer.write(start_line.encode("latin-1"))

    # headers
    for name, value in request.headers.items():
        header_line = f"{name}: {value}\r\n"
        writer.write(header_line.encode("latin-1"))

    # пустая строка = конец заголовков
    writer.write(b"\r\n")
    await writer.drain()


async def stream_body_fixed(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    length: int,
    timeouts: TimeoutConfig,
) -> None:
    """
    Стримит тело известной длины (Content-Length).
    
    Читаем чанками и сразу пишем — не буферизируем всё в память.
    drain() после каждого write — это backpressure.
    """
    remaining = length
    while remaining > 0:
        chunk_size = min(CHUNK_SIZE, remaining)
        chunk = await with_timeout(
            reader.read(chunk_size),
            timeouts.read,
            "reading body chunk"
        )
        if not chunk:
            raise ConnectionError("Client disconnected while sending body")

        writer.write(chunk)
        # drain() блокирует если получатель не успевает — это и есть backpressure
        await writer.drain()
        remaining -= len(chunk)


async def stream_body_chunked(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeouts: TimeoutConfig,
) -> None:
    """
    Стримит тело в chunked encoding.
    
    Формат:
    <size_hex>\r\n
    <data>\r\n
    ...
    0\r\n
    \r\n
    """
    while True:
        # читаем размер чанка (hex)
        size_line = await with_timeout(
            reader.readline(),
            timeouts.read,
            "reading chunk size"
        )
        if not size_line:
            raise ConnectionError("Client disconnected during chunked transfer")

        writer.write(size_line)

        try:
            chunk_size = int(size_line.strip(), 16)
        except ValueError:
            raise ValueError(f"Invalid chunk size: {size_line}")

        if chunk_size == 0:
            # финальный чанк — читаем trailing CRLF и выходим
            trailing = await reader.readline()
            writer.write(trailing)
            await writer.drain()
            break

        # читаем данные + CRLF после них
        remaining = chunk_size + 2  # +2 for \r\n
        while remaining > 0:
            to_read = min(CHUNK_SIZE, remaining)
            chunk = await with_timeout(
                reader.read(to_read),
                timeouts.read,
                "reading chunk data"
            )
            if not chunk:
                raise ConnectionError("Client disconnected during chunk")
            writer.write(chunk)
            await writer.drain()
            remaining -= len(chunk)


async def stream_response(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeouts: TimeoutConfig,
) -> int:
    """
    Читает ответ от upstream и стримит клиенту.
    
    Возвращает HTTP-статус для логов.
    """
    # status line: HTTP/1.1 200 OK
    status_line = await with_timeout(
        reader.readline(),
        timeouts.read,
        "reading response status"
    )
    if not status_line:
        raise ConnectionError("Upstream closed connection")

    writer.write(status_line)

    # парсим статус код
    try:
        parts = status_line.decode("latin-1").split(" ", 2)
        status_code = int(parts[1])
    except (IndexError, ValueError):
        status_code = 0

    # читаем заголовки, попутно ищем Content-Length или chunked
    content_length = None
    is_chunked = False

    while True:
        header_line = await with_timeout(
            reader.readline(),
            timeouts.read,
            "reading response header"
        )
        writer.write(header_line)

        if header_line in (b"\r\n", b"\n", b""):
            await writer.drain()
            break

        header_lower = header_line.decode("latin-1").lower()
        if header_lower.startswith("content-length:"):
            try:
                content_length = int(header_lower.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif header_lower.startswith("transfer-encoding:") and "chunked" in header_lower:
            is_chunked = True

    # стримим тело
    if content_length is not None:
        await stream_body_fixed(reader, writer, content_length, timeouts)
    elif is_chunked:
        await stream_body_chunked(reader, writer, timeouts)
    # else: нет тела (204, 304 и т.п.) или HTTP/1.0 без Content-Length

    return status_code


async def send_error(
    writer: asyncio.StreamWriter,
    status_code: int,
    message: str,
) -> None:
    """
    Отправляет клиенту страницу ошибки.
    
    Connection: close — после ошибки закрываем соединение.
    """
    body = f"<html><body><h1>{status_code} {message}</h1></body></html>"
    body_bytes = body.encode("utf-8")

    response = (
        f"HTTP/1.1 {status_code} {message}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )

    try:
        writer.write(response.encode("latin-1"))
        writer.write(body_bytes)
        await writer.drain()
    except Exception:
        pass  # клиент мог уже отвалиться
