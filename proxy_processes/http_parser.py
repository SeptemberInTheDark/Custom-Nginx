"""
Синхронный HTTP/1.1 парсер на blocking-сокетах.

Здесь, в отличие от async-версии и от proxy_threads_select,
читаем построчно через socket.makefile('rb'). settimeout()
на сокете — это и есть наш read timeout.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MAX_LINE = 8192
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
    method: str
    path: str
    version: str
    headers: Dict[str, str]
    raw_headers: List[Tuple[str, str]] = field(default_factory=list)

    @property
    def content_length(self) -> Optional[int]:
        cl = self.headers.get("content-length")
        return int(cl) if cl is not None else None

    @property
    def is_chunked(self) -> bool:
        return self.headers.get("transfer-encoding", "").lower() == "chunked"


def parse_request(rfile) -> HttpRequest:
    """Парсит request line + headers. Тело остаётся в потоке."""
    line = rfile.readline(MAX_LINE)
    if not line:
        raise ConnectionError("EMPTY")
    if len(line) >= MAX_LINE and not line.endswith(b"\n"):
        raise ValueError("Request line too long")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"Bad request line: {line!r}")
    method, path, version = parts
    headers, raw = parse_header_lines(rfile)
    return HttpRequest(method, path, version, headers, raw)


def parse_response_status_line(rfile) -> Tuple[str, int, str]:
    line = rfile.readline(MAX_LINE)
    if not line:
        raise ConnectionError("Upstream EOF")
    parts = line.decode("latin-1").rstrip("\r\n").split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"Bad status line: {line!r}")
    version = parts[0]
    try:
        status = int(parts[1])
    except ValueError:
        raise ValueError(f"Bad status code: {parts[1]!r}")
    reason = parts[2] if len(parts) > 2 else ""
    return version, status, reason


def parse_header_lines(rfile) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Читает блок headers до пустой строки."""
    headers: Dict[str, str] = {}
    raw: List[Tuple[str, str]] = []
    total = 0
    while True:
        line = rfile.readline(MAX_LINE)
        total += len(line)
        if total > MAX_HEADERS:
            raise ValueError("Headers too large")
        if line in (b"\r\n", b"\n", b""):
            return headers, raw
        text = line.decode("latin-1").rstrip("\r\n")
        name, sep, value = text.partition(":")
        if not sep:
            raise ValueError(f"Bad header: {text!r}")
        n = name.strip()
        v = value.strip()
        if n:
            headers[n.lower()] = v
            raw.append((n, v))
