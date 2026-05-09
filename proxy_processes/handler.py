"""
Синхронная обработка одного клиентского соединения.

Это аналог proxy/client_handler.py из asyncio-версии, только
полностью синхронный. Каждый клиент живёт в отдельном потоке
(см. worker.py), сокеты блокирующие, settimeout() даёт нам
все нужные таймауты.

Цикл keep-alive:
1. parse_request — заголовки от клиента
2. acquire — сокет к upstream (idle или новый)
3. forward request body (Content-Length / chunked)
4. read response status line + headers
5. forward response body (CL / chunked / close-delimited)
6. release — назад в пул либо close
7. если оба conn=keep-alive → goto 1
"""
import socket
import time
import uuid

from .http_parser import (
    HOP_BY_HOP,
    parse_header_lines,
    parse_request,
    parse_response_status_line,
)
from .logger import get_logger

log = get_logger()

CHUNK = 16 * 1024


def handle_client(cli: socket.socket, pool, timeouts) -> None:
    """Главный keep-alive цикл одного клиента."""
    try:
        cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass
    rfile = cli.makefile("rb", buffering=0)
    wfile = cli.makefile("wb", buffering=0)

    try:
        client_addr = cli.getpeername()
    except OSError:
        client_addr = "?"
    req_count = 0

    try:
        while True:
            req_count += 1
            trace_id = uuid.uuid4().hex[:8]
            t_start = time.monotonic()

            # таймаут на получение request line:
            # на первый запрос — parse, потом — keepalive_idle
            cli.settimeout(timeouts.parse if req_count == 1 else timeouts.keepalive_idle)
            try:
                req = parse_request(rfile)
            except socket.timeout:
                log.debug(f"[{trace_id}] keep-alive idle timeout")
                break
            except ConnectionError as e:
                if str(e) == "EMPTY":
                    log.debug(f"[{trace_id}] client EOF after {req_count - 1} reqs")
                else:
                    log.warning(f"[{trace_id}] read error: {e}")
                break
            except ValueError as e:
                log.warning(f"[{trace_id}] bad request: {e}")
                _send_error(wfile, 400, "Bad Request", trace_id)
                break

            # таймаут чтения тела + чтения ответа upstream'а
            cli.settimeout(timeouts.read)

            conn_h = req.headers.get("connection", "").lower()
            client_wants_close = (
                conn_h == "close"
                or (req.version.upper() == "HTTP/1.0" and conn_h != "keep-alive")
            )

            try:
                must_close, status, ups_addr = _proxy_one(
                    rfile, wfile, req, pool, timeouts, trace_id, client_wants_close
                )
            except TimeoutError as e:
                log.warning(f"[{trace_id}] timeout: {e}")
                _send_error(wfile, 504, "Gateway Timeout", trace_id)
                break
            except ConnectionError as e:
                log.warning(f"[{trace_id}] upstream conn error: {e}")
                _send_error(wfile, 502, "Bad Gateway", trace_id)
                break
            except OSError as e:
                log.warning(f"[{trace_id}] os error: {e}")
                _send_error(wfile, 502, "Bad Gateway", trace_id)
                break
            except Exception as e:
                log.exception(f"[{trace_id}] unexpected: {e}")
                _send_error(wfile, 500, "Internal Server Error", trace_id)
                break

            dt_ms = (time.monotonic() - t_start) * 1000
            level = log.warning if (dt_ms > 1000 or status >= 400) else log.info
            level(
                f"[{trace_id}] {req.method} {req.path} -> {ups_addr} "
                f"| {status} | {dt_ms:.1f}ms"
            )

            if must_close:
                break
    finally:
        for f in (rfile, wfile):
            try:
                f.close()
            except Exception:
                pass


def _proxy_one(rfile, wfile, req, pool, timeouts, trace_id, client_wants_close):
    """
    Обрабатывает один HTTP-запрос: пишет в upstream, читает ответ,
    возвращает (must_close_client, status_code, upstream_addr).
    """
    sock, addr = pool.acquire(timeouts.connect)
    upstream_reusable = False
    try:
        sock.settimeout(timeouts.read)
        up_rfile = sock.makefile("rb", buffering=0)
        up_wfile = sock.makefile("wb", buffering=0)

        # ── request ──────────────────────────────────────────────
        _write_request_head(up_wfile, req, trace_id)

        if req.content_length is not None and req.content_length > 0:
            _forward_fixed(rfile, up_wfile, req.content_length)
        elif req.is_chunked:
            _forward_chunked(rfile, up_wfile)
        up_wfile.flush()

        # ── response ─────────────────────────────────────────────
        version, status, reason = parse_response_status_line(up_rfile)
        resp_headers, raw_resp_headers = parse_header_lines(up_rfile)

        method = req.method.upper()
        no_body = method == "HEAD" or 100 <= status < 200 or status in (204, 304)
        cl_h = resp_headers.get("content-length")
        cl = int(cl_h) if cl_h is not None else None
        is_chunked = resp_headers.get("transfer-encoding", "").lower() == "chunked"
        upstream_close = "close" in resp_headers.get("connection", "").lower()
        close_delimited = (not no_body) and (cl is None) and (not is_chunked)

        must_close_client = client_wants_close or close_delimited
        upstream_reusable = (not upstream_close) and (not close_delimited)

        _write_response_head(wfile, version, status, reason, raw_resp_headers, must_close_client)

        if no_body:
            pass
        elif cl is not None:
            _forward_fixed(up_rfile, wfile, cl)
        elif is_chunked:
            _forward_chunked(up_rfile, wfile)
        else:
            _forward_until_eof(up_rfile, wfile)
        wfile.flush()

        return must_close_client, status, addr
    except BaseException:
        upstream_reusable = False
        raise
    finally:
        pool.release(addr, sock, upstream_reusable)


# ──────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────

def _write_request_head(wfile, req, trace_id: str) -> None:
    lines = [f"{req.method} {req.path} {req.version}".encode("latin-1")]
    seen_xtid = False
    for name, value in req.raw_headers:
        lname = name.lower()
        if lname in HOP_BY_HOP:
            continue
        if lname == "x-trace-id":
            value = trace_id
            seen_xtid = True
        lines.append(f"{name}: {value}".encode("latin-1"))
    if not seen_xtid:
        lines.append(f"X-Trace-Id: {trace_id}".encode("latin-1"))
    lines.append(b"Connection: keep-alive")
    wfile.write(b"\r\n".join(lines) + b"\r\n\r\n")


def _write_response_head(wfile, version, status, reason, raw_headers, must_close: bool) -> None:
    lines = [f"{version} {status} {reason}".encode("latin-1")]
    for name, value in raw_headers:
        if name.lower() in HOP_BY_HOP:
            continue
        lines.append(f"{name}: {value}".encode("latin-1"))
    lines.append(b"Connection: close" if must_close else b"Connection: keep-alive")
    wfile.write(b"\r\n".join(lines) + b"\r\n\r\n")


def _forward_fixed(src, dst, length: int) -> None:
    """Перекладываем ровно length байт src → dst."""
    remaining = length
    while remaining > 0:
        chunk = src.read(min(CHUNK, remaining))
        if not chunk:
            raise ConnectionError("EOF mid-body")
        dst.write(chunk)
        remaining -= len(chunk)


def _forward_chunked(src, dst) -> None:
    """Перекладываем chunked-тело. Trailers пропускаем как есть."""
    while True:
        size_line = src.readline(8192)
        if not size_line:
            raise ConnectionError("EOF mid-chunked")
        try:
            size = int(size_line.split(b";", 1)[0].strip(), 16)
        except ValueError:
            raise ConnectionError(f"bad chunk size: {size_line!r}")
        dst.write(size_line)
        if size == 0:
            # trailers до пустой строки
            while True:
                tr = src.readline(8192)
                dst.write(tr)
                if tr in (b"\r\n", b"\n", b""):
                    return
        # данные ровно size байт + \r\n
        remaining = size + 2
        while remaining > 0:
            chunk = src.read(min(CHUNK, remaining))
            if not chunk:
                raise ConnectionError("EOF mid-chunk-data")
            dst.write(chunk)
            remaining -= len(chunk)


def _forward_until_eof(src, dst) -> None:
    """Тело без CL и без chunked — стримим до закрытия upstream."""
    while True:
        chunk = src.read(CHUNK)
        if not chunk:
            return
        dst.write(chunk)


def _send_error(wfile, code: int, reason: str, trace_id: str) -> None:
    body = f"<html><body><h1>{code} {reason}</h1><p>trace: {trace_id}</p></body></html>".encode()
    head = (
        f"HTTP/1.1 {code} {reason}\r\n"
        f"Content-Type: text/html; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"X-Trace-Id: {trace_id}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode("latin-1")
    try:
        wfile.write(head + body)
        wfile.flush()
    except Exception:
        pass
