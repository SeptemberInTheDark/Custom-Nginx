"""
Минимальный HTTP/1.1 парсер.

Только то что нужно для проксирования:
- request line
- headers
- определение длины тела (Content-Length или chunked)

Тело не читаем — оно стримится отдельно.
"""
from dataclasses import dataclass
from typing import Dict, Optional
import asyncio


@dataclass
class HttpRequest:
    """
    Распарсенный HTTP-запрос (без тела).
    
    Headers хранятся в lowercase для удобства поиска.
    """
    method: str       # GET, POST, etc
    path: str         # /api/users?id=1
    version: str      # HTTP/1.1
    headers: Dict[str, str]

    @property
    def content_length(self) -> Optional[int]:
        """Длина тела или None если не указано."""
        cl = self.headers.get("content-length")
        return int(cl) if cl else None

    @property
    def is_chunked(self) -> bool:
        """Тело в chunked encoding?"""
        return self.headers.get("transfer-encoding", "").lower() == "chunked"

    @property
    def host(self) -> Optional[str]:
        """Host header."""
        return self.headers.get("host")


async def parse_request(reader: asyncio.StreamReader) -> HttpRequest:
    """
    Парсит request line и headers из потока.
    
    Формат HTTP/1.1:
    GET /path HTTP/1.1\r\n
    Host: example.com\r\n
    Content-Length: 42\r\n
    \r\n
    <body>
    
    Тело не читаем — остаётся в reader'е.
    """
    # первая строка: GET /path HTTP/1.1
    line = await reader.readline()
    if not line:
        raise ConnectionError("Empty request")

    # latin-1 — стандартная кодировка для HTTP/1.x headers
    parts = line.decode("latin-1").strip().split(" ", 2)
    if len(parts) != 3:
        raise ValueError(f"Malformed request line: {line}")

    method, path, version = parts

    # читаем заголовки до пустой строки
    headers: Dict[str, str] = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        # Host: example.com\r\n -> (Host, example.com)
        name, _, value = line.decode("latin-1").partition(":")
        # lowercase для удобства — "Content-Length" == "content-length"
        headers[name.strip().lower()] = value.strip()

    return HttpRequest(method, path, version, headers)
