"""
Инкрементальные HTTP/1.1 парсеры для request и response.

В отличие от proxy/utils/http.py (который async и читает из StreamReader),
здесь работаем с bytearray-буфером: feed() байты по мере поступления,
try_parse() возвращает результат когда заголовки целиком собрались.

После заголовков парсер не нужен — дальше байты тела стримятся
в conn.py как opaque bytes (с учётом Content-Length / chunked).
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_HEADERS = 64 * 1024
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}


@dataclass
class HttpRequest:
    method: str = ""
    path: str = ""
    version: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    raw_headers: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def content_length(self) -> Optional[int]:
        cl = self.headers.get("content-length")
        return int(cl) if cl is not None else None

    @property
    def is_chunked(self) -> bool:
        return self.headers.get("transfer-encoding", "").lower() == "chunked"


@dataclass
class HttpResponse:
    version: str = ""
    status: int = 0
    reason: str = ""
    headers: Dict[str, str] = field(default_factory=dict)
    raw_headers: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def content_length(self) -> Optional[int]:
        cl = self.headers.get("content-length")
        return int(cl) if cl is not None else None

    @property
    def is_chunked(self) -> bool:
        return self.headers.get("transfer-encoding", "").lower() == "chunked"

    @property
    def wants_close(self) -> bool:
        return "close" in self.headers.get("connection", "").lower()


def _parse_headers_block(block: bytes) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Парсит блок без request/status-line: lines, разделённые \\r\\n."""
    headers: Dict[str, str] = {}
    raw: List[Tuple[str, str]] = []
    if not block:
        return headers, raw
    for line in block.split(b"\r\n"):
        if not line:
            continue
        text = line.decode("latin-1")
        name, sep, value = text.partition(":")
        if not sep:
            raise ValueError(f"Bad header: {text!r}")
        n = name.strip()
        v = value.strip()
        if n:
            headers[n.lower()] = v
            raw.append((n, v))
    return headers, raw


def parse_request_headers(buf: bytearray):
    """
    Возвращает (HttpRequest, consumed_bytes) если headers целиком пришли,
    иначе None. Кидает ValueError при ошибке формата.
    """
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        if len(buf) > MAX_HEADERS:
            raise ValueError("Request headers too large")
        return None
    head = bytes(buf[:end])
    consumed = end + 4
    nl = head.find(b"\r\n")
    if nl < 0:
        first_line = head
        rest = b""
    else:
        first_line = head[:nl]
        rest = head[nl + 2:]
    parts = first_line.decode("latin-1").split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"Bad request line: {first_line!r}")
    method, path, version = parts
    headers, raw = _parse_headers_block(rest)
    return HttpRequest(method, path, version, headers, raw), consumed


def parse_response_headers(buf: bytearray):
    """Аналогично, но для status-line + headers."""
    end = buf.find(b"\r\n\r\n")
    if end < 0:
        if len(buf) > MAX_HEADERS:
            raise ValueError("Response headers too large")
        return None
    head = bytes(buf[:end])
    consumed = end + 4
    nl = head.find(b"\r\n")
    if nl < 0:
        first_line = head
        rest = b""
    else:
        first_line = head[:nl]
        rest = head[nl + 2:]
    parts = first_line.decode("latin-1").split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"Bad status line: {first_line!r}")
    version = parts[0]
    try:
        status = int(parts[1])
    except ValueError:
        raise ValueError(f"Bad status code: {parts[1]!r}")
    reason = parts[2] if len(parts) > 2 else ""
    headers, raw = _parse_headers_block(rest)
    return HttpResponse(version, status, reason, headers, raw), consumed


def build_request_head(req: HttpRequest, trace_id: str) -> bytes:
    """Сборка start-line + headers для отправки upstream'у.

    - режем hop-by-hop;
    - подкладываем X-Trace-Id (или перезаписываем существующий);
    - принудительно Connection: keep-alive (для пула upstream-сокетов).
    """
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
    return b"\r\n".join(lines) + b"\r\n\r\n"


def build_response_head(resp: HttpResponse, must_close: bool) -> bytes:
    """Сборка status-line + headers для отправки клиенту."""
    lines = [f"{resp.version} {resp.status} {resp.reason}".encode("latin-1")]
    for name, value in resp.raw_headers:
        if name.lower() in HOP_BY_HOP:
            continue
        lines.append(f"{name}: {value}".encode("latin-1"))
    lines.append(b"Connection: close" if must_close else b"Connection: keep-alive")
    return b"\r\n".join(lines) + b"\r\n\r\n"
