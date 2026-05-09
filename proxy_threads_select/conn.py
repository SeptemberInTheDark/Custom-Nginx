"""
HTTP-сессия одного клиента (state machine).

Замена client_handler.handle_client из asyncio-версии. Без await:
вместо ожидания на корутинах — конечный автомат, который двигает
event-loop из reactor.py при каждом IO-событии.

Поддерживает:
- HTTP/1.1 keep-alive с обеих сторон (клиент и upstream);
- request body: Content-Length и chunked (тело не парсится по байтам,
  только подсчитываются границы — pure passthrough);
- response body: Content-Length, chunked, close-delimited;
- non-blocking connect к upstream;
- дедлайны (поле .deadline; reactor проверяет в каждой итерации).

Состояния:
    READ_REQ_HEADERS  — копим заголовки запроса в cli_in
    PICK_UPSTREAM     — выбираем upstream, idle / новый
    CONNECT_UP        — ждём завершения non-blocking connect
    SEND_REQ          — пишем в upstream: заголовки + тело клиента
    READ_RESP_HEADERS — копим заголовки ответа в up_in
    SEND_RESP         — пишем клиенту: заголовки + тело upstream
    DONE              — один HTTP-цикл завершён
                        (либо closing, либо переход в READ_REQ_HEADERS)
    CLOSED            — сокет закрыт, сессия удаляется reactor'ом
"""
import errno
import socket
import time
import uuid
from selectors import EVENT_READ, EVENT_WRITE

from .http_parser import (
    parse_request_headers,
    parse_response_headers,
    build_request_head,
    build_response_head,
)
from .logger import get_logger

log = get_logger()

CHUNK = 16 * 1024


class S:
    READ_REQ_HEADERS = "read_req_headers"
    PICK_UPSTREAM = "pick_upstream"
    CONNECT_UP = "connect_up"
    SEND_REQ = "send_req"
    READ_RESP_HEADERS = "read_resp_headers"
    SEND_RESP = "send_resp"
    DONE = "done"
    CLOSED = "closed"


class Session:
    def __init__(self, client_sock, addr, reactor, pool, timeouts):
        # сокеты
        self.client = client_sock
        self.client_addr = addr
        self.upstream = None
        self.upstream_addr = None  # "host:port"
        self.upstream_was_idle = False  # пришёл из idle (в случае ошибки слот не освобождать)

        self.reactor = reactor
        self.pool = pool
        self.timeouts = timeouts

        # буферы
        self.cli_in = bytearray()
        self.cli_out = bytearray()
        self.up_in = bytearray()
        self.up_out = bytearray()

        # protocol state
        self.state = S.READ_REQ_HEADERS
        self.req = None
        self.resp = None
        self.trace_id = uuid.uuid4().hex[:8]

        # request body
        self.req_body_remaining = 0
        self.req_chunked = False
        self.req_body_done = True  # выставится в _step_read_req_headers

        # response body
        self.resp_body_remaining = 0
        self.resp_chunked = False
        self.resp_close_delimited = False
        self.resp_body_done = True
        self.resp_no_body = False

        self.client_wants_close = False
        self.must_close_after = False
        self.upstream_reusable = True

        # дедлайны и счётчики
        self.deadline = time.monotonic() + timeouts.parse
        self.req_count = 0
        self.req_started = 0.0

    # ──────────────────────────────────────────────────────────────────
    # регистрация в селекторе
    # ──────────────────────────────────────────────────────────────────

    def register_client_read(self) -> None:
        self.reactor.register(self.client, EVENT_READ, self._on_client_event)

    def _modify_client(self, want_read: bool, want_write: bool) -> None:
        events = (EVENT_READ if want_read else 0) | (EVENT_WRITE if want_write else 0)
        if events == 0:
            # selectors не любит 0-event; держим READ как минимальный (отлавливаем EOF)
            events = EVENT_READ
        self.reactor.modify(self.client, events, self._on_client_event)

    def _modify_upstream(self, want_read: bool, want_write: bool) -> None:
        if self.upstream is None:
            return
        events = (EVENT_READ if want_read else 0) | (EVENT_WRITE if want_write else 0)
        if events == 0:
            events = EVENT_READ
        self.reactor.modify(self.upstream, events, self._on_upstream_event)

    # ──────────────────────────────────────────────────────────────────
    # event-callbacks
    # ──────────────────────────────────────────────────────────────────

    def _on_client_event(self, mask: int) -> None:
        try:
            if mask & EVENT_READ:
                self._read_client()
            if self.state == S.CLOSED:
                return
            if mask & EVENT_WRITE:
                self._write_client()
            self._advance()
        except Exception as e:
            log.warning(f"[{self.trace_id}] client error: {type(e).__name__}: {e}")
            self.close()

    def _on_upstream_event(self, mask: int) -> None:
        try:
            if self.state == S.CONNECT_UP:
                self._finish_connect()
            if self.state == S.CLOSED or self.upstream is None:
                return
            if mask & EVENT_READ:
                self._read_upstream()
            if self.state == S.CLOSED or self.upstream is None:
                return
            if mask & EVENT_WRITE:
                self._write_upstream()
            self._advance()
        except Exception as e:
            log.warning(f"[{self.trace_id}] upstream error: {type(e).__name__}: {e}")
            self._send_error_and_close(502, "Bad Gateway")

    # ──────────────────────────────────────────────────────────────────
    # IO примитивы
    # ──────────────────────────────────────────────────────────────────

    def _read_client(self) -> None:
        try:
            data = self.client.recv(CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except (ConnectionResetError, OSError):
            self.close()
            return
        if not data:
            # клиент закрыл сокет
            if self.state == S.READ_REQ_HEADERS and not self.cli_in:
                self.close()
                return
            raise ConnectionError("client EOF")
        self.cli_in.extend(data)

    def _write_client(self) -> None:
        if not self.cli_out:
            return
        try:
            n = self.client.send(self.cli_out)
        except (BlockingIOError, InterruptedError):
            return
        except (BrokenPipeError, ConnectionResetError, OSError):
            self.close()
            return
        if n > 0:
            del self.cli_out[:n]

    def _read_upstream(self) -> None:
        try:
            data = self.upstream.recv(CHUNK)
        except (BlockingIOError, InterruptedError):
            return
        except (ConnectionResetError, OSError) as e:
            raise ConnectionError(f"upstream recv: {e}")
        if not data:
            # EOF от upstream
            self.upstream_reusable = False
            if self.state == S.SEND_RESP and self.resp_close_delimited and not self.resp_body_done:
                self.resp_body_done = True
                return
            if self.state in (S.READ_RESP_HEADERS, S.SEND_RESP):
                raise ConnectionError("upstream EOF mid-response")
            return
        self.up_in.extend(data)

    def _write_upstream(self) -> None:
        if not self.up_out:
            return
        try:
            n = self.upstream.send(self.up_out)
        except (BlockingIOError, InterruptedError):
            return
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            raise ConnectionError(f"upstream send: {e}")
        if n > 0:
            del self.up_out[:n]

    # ──────────────────────────────────────────────────────────────────
    # конечный автомат
    # ──────────────────────────────────────────────────────────────────

    def _advance(self) -> None:
        """
        Двигаем состояние пока возможно — без блокировок.
        Если состояние не изменилось за итерацию, ждём IO.
        """
        while True:
            prev = self.state
            if self.state == S.READ_REQ_HEADERS:
                self._step_read_req_headers()
            elif self.state == S.PICK_UPSTREAM:
                self._step_pick_upstream()
            elif self.state == S.CONNECT_UP:
                self._update_interest()
                return
            elif self.state == S.SEND_REQ:
                self._step_send_req()
            elif self.state == S.READ_RESP_HEADERS:
                self._step_read_resp_headers()
            elif self.state == S.SEND_RESP:
                self._step_send_resp()
            elif self.state == S.DONE:
                self._step_done()
            elif self.state == S.CLOSED:
                return

            if self.state == prev:
                self._update_interest()
                return

    # — шаги —

    def _step_read_req_headers(self) -> None:
        if not self.cli_in:
            return
        try:
            result = parse_request_headers(self.cli_in)
        except ValueError:
            self._send_error_and_close(400, "Bad Request")
            return
        if result is None:
            return  # ждём ещё байты
        req, consumed = result
        del self.cli_in[:consumed]
        self.req = req
        self.req_count += 1
        self.req_started = time.monotonic()
        self.deadline = self.req_started + self.timeouts.total

        # определяем тело запроса
        cl = req.content_length
        if cl is not None:
            self.req_body_remaining = cl
            self.req_chunked = False
            self.req_body_done = (cl == 0)
        elif req.is_chunked:
            self.req_body_remaining = 0
            self.req_chunked = True
            self.req_body_done = False
        else:
            self.req_body_remaining = 0
            self.req_chunked = False
            self.req_body_done = True

        # хочет ли клиент закрыть после ответа
        conn_h = req.headers.get("connection", "").lower()
        self.client_wants_close = (
            conn_h == "close"
            or (req.version.upper() == "HTTP/1.0" and conn_h != "keep-alive")
        )

        # сразу формируем headers для отправки upstream'у
        self.up_out.extend(build_request_head(req, self.trace_id))
        self.state = S.PICK_UPSTREAM

    def _step_pick_upstream(self) -> None:
        u = self.pool.pick_upstream()
        addr = u.address

        # 1) пробуем idle keep-alive
        sock = self.pool.take_idle(addr)
        if sock is not None:
            self.upstream = sock
            self.upstream_addr = addr
            self.upstream_was_idle = True
            try:
                self.upstream.setblocking(False)
            except OSError:
                self._discard_upstream()
                # деградируем до открытия нового
            else:
                self.reactor.register(self.upstream, EVENT_WRITE, self._on_upstream_event)
                self.state = S.SEND_REQ
                return

        # 2) открываем новый
        if not self.pool.acquire_slot(addr):
            log.warning(f"[{self.trace_id}] upstream {addr} pool exhausted")
            self._send_error_and_close(503, "Service Unavailable")
            return

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

        self.upstream = sock
        self.upstream_addr = addr
        self.upstream_was_idle = False

        try:
            err = sock.connect_ex((u.host, u.port))
        except OSError as e:
            err = e.errno

        if err == 0:
            # уже подключились синхронно (редко)
            self.reactor.register(self.upstream, EVENT_WRITE, self._on_upstream_event)
            self.state = S.SEND_REQ
            return
        if err in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
            self.reactor.register(self.upstream, EVENT_WRITE, self._on_upstream_event)
            self.state = S.CONNECT_UP
            self.deadline = time.monotonic() + self.timeouts.connect
            return

        # реально не получилось
        self._discard_upstream()
        self._send_error_and_close(502, "Bad Gateway")

    def _finish_connect(self) -> None:
        err = self.upstream.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
        if err != 0:
            log.warning(f"[{self.trace_id}] upstream connect failed: errno={err}")
            self._discard_upstream()
            self._send_error_and_close(502, "Bad Gateway")
            return
        self.state = S.SEND_REQ
        self.deadline = time.monotonic() + self.timeouts.total

    def _step_send_req(self) -> None:
        # переливаем тело клиента (если есть) в буфер upstream'а
        self._pump_request_body()
        if self.up_out:
            return  # ждём writable upstream
        if self.req_body_done:
            self.state = S.READ_RESP_HEADERS

    def _pump_request_body(self) -> None:
        """cli_in -> up_out согласно режиму тела (CL / chunked)."""
        if self.req_body_done:
            return
        if not self.req_chunked:
            n = min(len(self.cli_in), self.req_body_remaining)
            if n > 0:
                self.up_out.extend(self.cli_in[:n])
                del self.cli_in[:n]
                self.req_body_remaining -= n
            if self.req_body_remaining == 0:
                self.req_body_done = True
            return
        # chunked
        while True:
            nl = self.cli_in.find(b"\r\n")
            if nl < 0:
                return
            size_line = bytes(self.cli_in[:nl + 2])
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise ConnectionError("bad chunk size from client")
            if size == 0:
                # завершающий 0\r\n + (опционально trailers) + \r\n
                # упрощение: ждём обязательный финальный \r\n
                need = nl + 2 + 2
                if len(self.cli_in) < need:
                    return
                self.up_out.extend(self.cli_in[:need])
                del self.cli_in[:need]
                self.req_body_done = True
                return
            need = nl + 2 + size + 2
            if len(self.cli_in) < need:
                return
            self.up_out.extend(self.cli_in[:need])
            del self.cli_in[:need]

    def _step_read_resp_headers(self) -> None:
        if not self.up_in:
            return
        try:
            result = parse_response_headers(self.up_in)
        except ValueError:
            self._send_error_and_close(502, "Bad Gateway")
            return
        if result is None:
            return
        resp, consumed = result
        del self.up_in[:consumed]
        self.resp = resp

        method = self.req.method.upper() if self.req else "GET"
        no_body = (
            method == "HEAD"
            or 100 <= resp.status < 200
            or resp.status in (204, 304)
        )
        cl = resp.content_length
        is_chunked = resp.is_chunked
        upstream_close = resp.wants_close

        self.resp_no_body = no_body
        if no_body:
            self.resp_body_done = True
            self.resp_chunked = False
            self.resp_close_delimited = False
            self.resp_body_remaining = 0
        elif cl is not None:
            self.resp_body_remaining = cl
            self.resp_chunked = False
            self.resp_close_delimited = False
            self.resp_body_done = (cl == 0)
        elif is_chunked:
            self.resp_chunked = True
            self.resp_body_remaining = 0
            self.resp_close_delimited = False
            self.resp_body_done = False
        else:
            self.resp_chunked = False
            self.resp_close_delimited = True
            self.resp_body_done = False

        self.must_close_after = self.client_wants_close or self.resp_close_delimited
        self.upstream_reusable = (not upstream_close) and (not self.resp_close_delimited)

        self.cli_out.extend(build_response_head(resp, self.must_close_after))
        self.state = S.SEND_RESP

    def _step_send_resp(self) -> None:
        self._pump_response_body()
        if self.cli_out:
            return  # ждём writable клиента
        if self.resp_body_done:
            self.state = S.DONE

    def _pump_response_body(self) -> None:
        if self.resp_body_done:
            # close-delimited могли получить EOF, но в up_in остались байты — слить
            if self.up_in:
                self.cli_out.extend(self.up_in)
                self.up_in.clear()
            return
        if self.resp_close_delimited:
            if self.up_in:
                self.cli_out.extend(self.up_in)
                self.up_in.clear()
            # завершение придёт при EOF в _read_upstream
            return
        if not self.resp_chunked:
            n = min(len(self.up_in), self.resp_body_remaining)
            if n > 0:
                self.cli_out.extend(self.up_in[:n])
                del self.up_in[:n]
                self.resp_body_remaining -= n
            if self.resp_body_remaining == 0:
                self.resp_body_done = True
            return
        # chunked
        while True:
            nl = self.up_in.find(b"\r\n")
            if nl < 0:
                return
            size_line = bytes(self.up_in[:nl + 2])
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise ConnectionError("bad chunk size from upstream")
            if size == 0:
                need = nl + 2 + 2
                if len(self.up_in) < need:
                    return
                self.cli_out.extend(self.up_in[:need])
                del self.up_in[:need]
                self.resp_body_done = True
                return
            need = nl + 2 + size + 2
            if len(self.up_in) < need:
                return
            self.cli_out.extend(self.up_in[:need])
            del self.up_in[:need]

    def _step_done(self) -> None:
        # лог запроса
        if self.req is not None and self.resp is not None:
            dt_ms = (time.monotonic() - self.req_started) * 1000
            level = log.warning if (dt_ms > 1000 or self.resp.status >= 400) else log.info
            level(
                f"[{self.trace_id}] {self.req.method} {self.req.path} -> "
                f"{self.upstream_addr} | {self.resp.status} | {dt_ms:.1f}ms"
            )

        # отдаём upstream обратно в пул либо закрываем
        self._release_upstream(self.upstream_reusable)

        if self.must_close_after:
            # ждём, пока добьём оставшийся cli_out, потом close
            if self.cli_out:
                # state остаётся DONE; _update_interest поставит EVENT_WRITE
                return
            self.close()
            return

        # keep-alive: новый цикл
        self.req = None
        self.resp = None
        self.up_in.clear()
        self.up_out.clear()  # на всякий
        self.req_body_done = True
        self.resp_body_done = True
        self.resp_no_body = False
        self.resp_close_delimited = False
        self.resp_chunked = False
        self.req_chunked = False
        self.upstream_reusable = True
        self.must_close_after = False
        self.client_wants_close = False
        self.trace_id = uuid.uuid4().hex[:8]
        self.deadline = time.monotonic() + self.timeouts.keepalive_idle
        self.state = S.READ_REQ_HEADERS

    # ──────────────────────────────────────────────────────────────────
    # selector interest
    # ──────────────────────────────────────────────────────────────────

    def _update_interest(self) -> None:
        if self.state == S.CLOSED:
            return

        # клиентская сторона
        cli_read = False
        cli_write = bool(self.cli_out)
        if self.state == S.READ_REQ_HEADERS:
            cli_read = True
        elif self.state == S.SEND_REQ and not self.req_body_done:
            cli_read = True
        elif self.state == S.SEND_RESP:
            # параллельно отлавливаем EOF клиента
            cli_read = True
        elif self.state == S.DONE and self.must_close_after and self.cli_out:
            cli_write = True
        try:
            self._modify_client(cli_read, cli_write)
        except (KeyError, ValueError, OSError):
            pass

        # upstream сторона
        if self.upstream is None:
            return
        up_read = False
        up_write = bool(self.up_out)
        if self.state == S.CONNECT_UP:
            up_write = True
        elif self.state in (S.READ_RESP_HEADERS, S.SEND_RESP):
            up_read = True
        try:
            self._modify_upstream(up_read, up_write)
        except (KeyError, ValueError, OSError):
            pass

    # ──────────────────────────────────────────────────────────────────
    # ошибки и закрытие
    # ──────────────────────────────────────────────────────────────────

    def _send_error_and_close(self, code: int, reason: str) -> None:
        body = f"<html><body><h1>{code} {reason}</h1><p>trace: {self.trace_id}</p></body></html>".encode()
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: text/html; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"X-Trace-Id: {self.trace_id}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("latin-1")
        self.cli_out[:0] = head + body  # вперёд всех буферов
        self.must_close_after = True
        try:
            self._write_client()
        except Exception:
            pass
        # отпускаем upstream если был открыт
        self._release_upstream(reusable=False)
        self.close()

    def _release_upstream(self, reusable: bool) -> None:
        if self.upstream is None:
            return
        sock = self.upstream
        addr = self.upstream_addr
        was_idle = self.upstream_was_idle
        try:
            self.reactor.unregister(sock)
        except (KeyError, ValueError, OSError):
            pass
        if reusable:
            try:
                self.pool.put_idle(addr, sock)
            except Exception:
                try:
                    sock.close()
                except Exception:
                    pass
                # idle пришедший сокет: слот уже был, отпускаем
                self.pool.release_slot(addr)
        else:
            try:
                sock.close()
            except Exception:
                pass
            self.pool.release_slot(addr)
        self.upstream = None
        self.upstream_addr = None
        self.upstream_was_idle = False

    def _discard_upstream(self) -> None:
        """Закрыть upstream-сокет и освободить слот, если был занят."""
        if self.upstream is None:
            return
        try:
            self.reactor.unregister(self.upstream)
        except (KeyError, ValueError, OSError):
            pass
        try:
            self.upstream.close()
        except Exception:
            pass
        if not self.upstream_was_idle and self.upstream_addr is not None:
            self.pool.release_slot(self.upstream_addr)
        self.upstream = None
        self.upstream_addr = None
        self.upstream_was_idle = False

    def close(self) -> None:
        if self.state == S.CLOSED:
            return
        self.state = S.CLOSED
        try:
            self.reactor.unregister(self.client)
        except (KeyError, ValueError, OSError):
            pass
        try:
            self.client.close()
        except Exception:
            pass
        # если upstream ещё держим — закрываем (без reuse)
        if self.upstream is not None:
            self._release_upstream(reusable=False)
