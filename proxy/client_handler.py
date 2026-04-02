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
from proxy.upstream_pool import UpstreamPool, Upstream
from proxy.timeouts import with_timeout
from proxy.utils.http import HttpRequest, parse_request
from proxy.logger import generate_trace_id, set_trace_id

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

            upstream_info = "unknown"

            try:
                # парсим только заголовки, тело будем стримить
                parse_start = time.time()  # ВРЕМЯ НА ПАРСИНГ ЗАГОЛОВКОВ
                request = await with_timeout(
                    parse_request(client_reader),
                    timeouts.parse,
                    "parsing request",
                )
                parse_ms = (
                    time.time() - parse_start
                ) * 1000  # ВРЕМЯ НА ПАРСИНГ ЗАГОЛОВКОВ В МС

                logger.debug(
                    f"[{request_count}] {request.method} {request.path} from {client_addr}"
                )

                # проверяем просит ли клиент закрыть соединение
                client_wants_close = (
                    request.headers.get("connection", "").lower() == "close"
                )

                # берём соединение из пула
                connect_start = time.time()  # ВРЕМЯ НА УСТАНОВКУ СОЕДИНЕНИЯ С UPSTREAM
                async with upstream_pool.acquire_connection(timeouts.connect) as (
                    up_reader,
                    up_writer,
                    upstream,
                ):
                    connect_ms = (
                        time.time() - connect_start
                    ) * 1000  # ВРЕМЯ НА УСТАНОВКУ СОЕДИНЕНИЯ С UPSTREAM В МС
                    upstream_info = upstream.address
                    logger.debug(f"[{request_count}] -> upstream {upstream_info}")

                    # отправляем заголовки запроса + добавляем X-Trace-Id
                    await forward_request_headers(
                        request, up_writer, timeouts, trace_id
                    )

                    # стримим тело запроса если есть
                    if request.content_length:
                        await stream_body_fixed(
                            client_reader, up_writer, request.content_length, timeouts
                        )
                    elif request.is_chunked:
                        await stream_body_chunked(client_reader, up_writer, timeouts)

                    await up_writer.drain()

                    # получаем и стримим ответ
                    stream_start = (
                        time.time()
                    )  # ВРЕМЯ НА ПОЛУЧЕНИЕ И СТРИМИНГ ОТВЕТА ОТ UPSTREAM
                    status_code, upstream_wants_close = await stream_response(
                        up_reader, client_writer, timeouts
                    )
                    stream_ms = (
                        time.time() - stream_start
                    ) * 1000  # ВРЕМЯ НА ПОЛУЧЕНИЕ И СТРИМИНГ ОТВЕТА ОТ UPSTREAM В МС
                    total_ms = (
                        time.time() - req_start
                    ) * 1000  # ОБЩЕЕ ВРЕМЯ НА ОБРАБОТКУ ЗАПРОСА В МС
                    logger.info(
                        f"[{request_count}] {request.method} {request.path} -> {upstream_info} | {status_code} | "
                        f"timing: parse={parse_ms:.1f}ms connect={connect_ms:.1f}ms stream={stream_ms:.1f}ms total={total_ms:.1f}ms"  # ← ИЗМЕНИТЬ
                    )

                # решаем: закрыть соединение или нет
                should_close = client_wants_close or upstream_wants_close
                if not should_close:
                    logger.debug(
                        f"[{request_count}] keep-alive: waiting for next request"
                    )
                    continue
                else:
                    logger.debug(f"[{request_count}] closing keep-alive connection")
                    break

            except TimeoutError as e:
                logger.warning(f"[{request_count}] Timeout: {e}")
                await send_error(client_writer, 504, "Gateway Timeout", trace_id)
                break
            except ConnectionError as e:
                # если это ошибка при парсинге первого запроса, это может быть EOF
                if request_count == 1:
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
    # request line
    start_line = f"{request.method} {request.path} {request.version}\r\n"
    writer.write(start_line.encode("latin-1"))

    # оригинальные headers
    for name, value in request.headers.items():
        header_line = f"{name}: {value}\r\n"
        writer.write(header_line.encode("latin-1"))

    # добавляем trace_id — upstream тоже сможет его логировать
    writer.write(f"x-trace-id: {trace_id}\r\n".encode("latin-1"))

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
            reader.read(chunk_size), timeouts.read, "reading body chunk"
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
            reader.readline(), timeouts.read, "reading chunk size"
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
                reader.read(to_read), timeouts.read, "reading chunk data"
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
) -> tuple:
    """
    Читает ответ от upstream и стримит клиенту.

    Возвращает (status_code, wants_close):
    - status_code: HTTP-статус для логов
    - wants_close: True если в ответе есть "Connection: close"
    """
    # status line: HTTP/1.1 200 OK
    status_line = await with_timeout(
        reader.readline(), timeouts.read, "reading response status"
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

    # читаем заголовки, ищем Content-Length, chunked и Connection
    content_length = None
    is_chunked = False
    upstream_wants_close = False

    while True:
        header_line = await with_timeout(
            reader.readline(), timeouts.read, "reading response header"
        )
        writer.write(header_line)

        if header_line in (b"\r\n", b"\n", b""):
            await writer.drain()
            break

        header_lower = header_line.decode("latin-1", errors="ignore").lower()
        if header_lower.startswith("content-length:"):
            try:
                content_length = int(header_lower.split(":", 1)[1].strip())
            except ValueError:
                pass
        elif (
            header_lower.startswith("transfer-encoding:") and "chunked" in header_lower
        ):
            is_chunked = True
        elif header_lower.startswith("connection:") and "close" in header_lower:
            upstream_wants_close = True

    # стримим тело
    if content_length is not None:
        await stream_body_fixed(reader, writer, content_length, timeouts)
    elif is_chunked:
        await stream_body_chunked(reader, writer, timeouts)
    # else: нет тела (204, 304 и т.п.) или HTTP/1.0 без Content-Length

    return status_code, upstream_wants_close


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
