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
import socket
import time
from proxy.config import TimeoutConfig
from proxy.upstream_pool import UpstreamPool
from proxy.timeouts import drain_with_timeout, with_timeout
from proxy.utils.http import HttpRequest, parse_header_line, parse_request
from proxy.logger import generate_trace_id, set_trace_id

logger = logging.getLogger("proxy")

# 16KB — хороший баланс между latency и throughput
# меньше — больше syscall'ов, больше — дольше ждём первый чанк
CHUNK_SIZE = 16 * 1024
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "upgrade",
}


class KeepAliveIdleTimeout(Exception):
    """Клиент оставил keep-alive соединение без нового запроса."""


async def handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_pool: UpstreamPool,
    timeouts: TimeoutConfig,
) -> None:
    """
    Обрабатывает HTTP-запросы с поддержкой keep-alive.

    Цикл keep-alive:
    1. парсим заголовки запроса
    2. коннектимся к upstream
    3. стримим данные туда-сюда
    4. проверяем "Connection" в ответе
    5. если close — выходим, иначе ждём следующий запрос

    Trace-ID обновляется для каждого запроса в одном соединении.
    """
    client_addr = client_writer.get_extra_info("peername")
    request_count = 0

    # Оптимизируем client сокет для производительности
    sock = client_writer.get_extra_info("socket")
    if sock:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except (AttributeError, OSError):
            pass

    try:
        while True:
            # генерируем новый trace_id для каждого запроса в цепочке
            trace_id = generate_trace_id()
            set_trace_id(trace_id)
            request_count += 1
            req_start = time.time()

            try:
                should_close = await with_timeout(
                    proxy_one_request(
                        client_reader,
                        client_writer,
                        upstream_pool,
                        timeouts,
                        trace_id,
                        request_count,
                        req_start,
                    ),
                    timeouts.total,
                    "processing request",
                )
                if not should_close:
                    continue
                else:
                    break

            except TimeoutError as e:
                logger.warning(f"[{request_count}] Timeout: {e}")
                await send_error(client_writer, 504, "Gateway Timeout", trace_id)
                break
            except KeepAliveIdleTimeout:
                logger.debug(f"[{request_count}] Keep-alive idle timeout")
                break
            except ConnectionError as e:
                # если это ошибка при парсинге первого запроса, это может быть EOF
                if str(e) == "Empty request":
                    logger.debug(f"[{request_count}] Client disconnected (EOF)")
                    break
                logger.warning(f"[{request_count}] Connection error: {e}")
                await send_error(client_writer, 502, "Bad Gateway", trace_id)
                break
            except Exception as e:
                logger.exception(f"[{request_count}] Unexpected error: {e}")
                await send_error(client_writer, 500, "Internal Server Error", trace_id)
                break

    finally:
        # закрываем соединение с клиентом
        try:
            client_writer.close()
            await client_writer.wait_closed()
        except Exception:
            pass
        logger.debug(f"Connection from {client_addr} closed ({request_count} requests)")


async def proxy_one_request(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_pool: UpstreamPool,
    timeouts: TimeoutConfig,
    trace_id: str,
    request_count: int,
    req_start: float,
) -> bool:
    """Обрабатывает один HTTP-запрос внутри client keep-alive соединения."""
    parse_start = time.time()
    try:
        request = await with_timeout(
            parse_request(client_reader),
            timeouts.parse,
            "parsing request",
        )
    except TimeoutError:
        if request_count > 1:
            raise KeepAliveIdleTimeout()
        raise
    parse_ms = (time.time() - parse_start) * 1000

    connection_header = request.headers.get("connection", "").lower()
    client_wants_close = connection_header == "close" or (
        request.version.upper() == "HTTP/1.0" and connection_header != "keep-alive"
    )
    upstream_info = "unknown"

    connect_start = time.time()
    async with upstream_pool.acquire_connection(timeouts.connect) as upstream_conn:
        connect_ms = (time.time() - connect_start) * 1000
        up_reader = upstream_conn.reader
        up_writer = upstream_conn.writer
        upstream_info = upstream_conn.upstream.address

        await forward_request_headers(request, up_writer, timeouts, trace_id)

        if request.content_length:
            await stream_body_fixed(
                client_reader, up_writer, request.content_length, timeouts
            )
        elif request.is_chunked:
            await stream_body_chunked(client_reader, up_writer, timeouts)

        await drain_with_timeout(up_writer, timeouts.write, "flushing request")

        stream_start = time.time()
        status_code, must_close_client, can_reuse_upstream = await stream_response(
            up_reader, client_writer, timeouts, request.method, client_wants_close
        )
        upstream_conn.reusable = can_reuse_upstream
        stream_ms = (time.time() - stream_start) * 1000
        total_ms = (time.time() - req_start) * 1000

        if total_ms > 1000 or status_code >= 400:
            logger.warning(
                f"[{request_count}] {request.method} {request.path} -> {upstream_info} | {status_code} | "
                f"timing: parse={parse_ms:.1f}ms connect={connect_ms:.1f}ms stream={stream_ms:.1f}ms total={total_ms:.1f}ms"
            )

    return must_close_client


async def forward_request_headers(
    request: HttpRequest,
    writer: asyncio.StreamWriter,
    timeouts: TimeoutConfig,
    trace_id: str,
) -> None:
    """
    Пересылает HTTP-заголовки upstream'у.

    Добавляет X-Trace-Id для сквозной трассировки.

    Формат HTTP/1.1:
    GET /path HTTP/1.1\r\n
    Host: example.com\r\n
    X-Trace-Id: abc12345\r\n
    \r\n
    """
    start_line = f"{request.method} {request.path} {request.version}\r\n"
    writer.write(start_line.encode("latin-1"))

    for name, value in request.headers.items():
        if name in HOP_BY_HOP_HEADERS:
            continue
        if name == "x-trace-id":
            continue
        header_line = f"{name}: {value}\r\n"
        writer.write(header_line.encode("latin-1"))

    writer.write(f"x-trace-id: {trace_id}\r\n".encode("latin-1"))
    writer.write(b"connection: keep-alive\r\n")

    writer.write(b"\r\n")
    await drain_with_timeout(writer, timeouts.write, "writing request headers")


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
            reader.read(chunk_size), timeouts.read, "reading body chunk"
        )
        if not chunk:
            raise ConnectionError("Client disconnected while sending body")

        writer.write(chunk)
        await drain_with_timeout(writer, timeouts.write, "writing body chunk")
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
            reader.readline(), timeouts.read, "reading chunk size"
        )
        if not size_line:
            raise ConnectionError("Client disconnected during chunked transfer")

        writer.write(size_line)

        try:
            chunk_size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise ValueError(f"Invalid chunk size: {size_line}")

        if chunk_size == 0:
            while True:
                trailing = await with_timeout(
                    reader.readline(), timeouts.read, "reading chunk trailer"
                )
                writer.write(trailing)
                if trailing in (b"\r\n", b"\n", b""):
                    break
            await drain_with_timeout(writer, timeouts.write, "writing chunk trailer")
            break

        # читаем данные + CRLF после них
        remaining = chunk_size + 2  # +2 for \r\n
        while remaining > 0:
            to_read = min(CHUNK_SIZE, remaining)
            chunk = await with_timeout(
                reader.read(to_read), timeouts.read, "reading chunk data"
            )
            if not chunk:
                raise ConnectionError("Client disconnected during chunk")
            writer.write(chunk)
            await drain_with_timeout(writer, timeouts.write, "writing chunk data")
            remaining -= len(chunk)


async def stream_response(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeouts: TimeoutConfig,
    request_method: str,
    client_wants_close: bool,
) -> tuple[int, bool, bool]:
    """
    Читает ответ от upstream и стримит клиенту.

    Возвращает (status_code, must_close_client, can_reuse_upstream):
    - status_code: HTTP-статус для логов
    - must_close_client: True если клиентское соединение нельзя держать открытым
    - can_reuse_upstream: True если upstream-сокет можно вернуть в keep-alive пул
    """
    # status line: HTTP/1.1 200 OK
    status_line = await with_timeout(
        reader.readline(), timeouts.read, "reading response status"
    )
    if not status_line:
        raise ConnectionError("Upstream closed connection")

    # парсим статус код
    try:
        parts = status_line.decode("latin-1").split(" ", 2)
        status_code = int(parts[1])
    except (IndexError, ValueError):
        status_code = 0

    # читаем заголовки, ищем Content-Length, chunked и Connection
    content_length = None
    is_chunked = False
    upstream_wants_close = False
    response_headers = []

    while True:
        header_line = await with_timeout(
            reader.readline(), timeouts.read, "reading response header"
        )

        if header_line in (b"\r\n", b"\n", b""):
            break

        header_name, header_value = parse_header_line(header_line)
        if header_name == "content-length":
            try:
                content_length = int(header_value)
            except ValueError:
                pass
        elif header_name == "transfer-encoding" and "chunked" in header_value.lower():
            is_chunked = True
        elif header_name == "connection" and "close" in header_value.lower():
            upstream_wants_close = True

        if header_name not in HOP_BY_HOP_HEADERS:
            response_headers.append(header_line)

    no_body = (
        request_method.upper() == "HEAD"
        or 100 <= status_code < 200
        or status_code in (204, 304)
    )
    close_delimited = (
        not no_body and content_length is None and not is_chunked
    )
    must_close_client = client_wants_close or close_delimited
    can_reuse_upstream = not upstream_wants_close and not close_delimited

    writer.write(status_line)
    for header_line in response_headers:
        writer.write(header_line)
    if must_close_client:
        writer.write(b"Connection: close\r\n")
    else:
        writer.write(b"Connection: keep-alive\r\n")
    writer.write(b"\r\n")
    await drain_with_timeout(writer, timeouts.write, "writing response headers")

    # стримим тело
    if no_body:
        pass
    elif content_length is not None:
        await stream_body_fixed(reader, writer, content_length, timeouts)
    elif is_chunked:
        await stream_body_chunked(reader, writer, timeouts)
    elif close_delimited:
        await stream_until_eof(reader, writer, timeouts)

    return status_code, must_close_client, can_reuse_upstream


async def stream_until_eof(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    timeouts: TimeoutConfig,
) -> None:
    """Стримит тело ответа, где граница определяется закрытием upstream-соединения."""
    while True:
        chunk = await with_timeout(
            reader.read(CHUNK_SIZE), timeouts.read, "reading close-delimited body"
        )
        if not chunk:
            break
        writer.write(chunk)
        await drain_with_timeout(writer, timeouts.write, "writing close-delimited body")


async def send_error(
    writer: asyncio.StreamWriter,
    status_code: int,
    message: str,
    trace_id: str,
) -> None:
    """
    Отправляет клиенту страницу ошибки.

    Включает trace_id в заголовок X-Trace-Id для отладки.
    Connection: close — после ошибки закрываем соединение.
    """
    body = f"<html><body><h1>{status_code} {message}</h1><p>trace: {trace_id}</p></body></html>"
    body_bytes = body.encode("utf-8")

    response = (
        f"HTTP/1.1 {status_code} {message}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body_bytes)}\r\n"
        f"X-Trace-Id: {trace_id}\r\n"
        f"Connection: close\r\n"
        f"\r\n"
    )

    try:
        writer.write(response.encode("latin-1"))
        writer.write(body_bytes)
        await writer.drain()
    except Exception:
        pass  # клиент мог уже отвалиться
