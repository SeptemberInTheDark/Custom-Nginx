from dataclasses import dataclass
from typing import Dict, Optional
import asyncio

@dataclass
class HttpRequest:
    method: str
    path: str
    version: str
    headers: Dict[str, str]
    # Тело НЕ храним — стримим напрямую
    
    @property
    def content_length(self) -> Optional[int]:
        cl = self.headers.get("content-length")
        return int(cl) if cl else None
    
    @property
    def is_chunked(self) -> bool:
        return self.headers.get("transfer-encoding", "").lower() == "chunked"

async def parse_request(reader: asyncio.StreamReader) -> HttpRequest:
    """Парсит стартовую строку и заголовки, НЕ читает тело."""
    # Читаем стартовую строку
    line = await reader.readline()
    if not line:
        raise ConnectionError("Empty request")
    
    # b"GET /path HTTP/1.1\r\n" -> ["GET", "/path", "HTTP/1.1"]
    parts = line.decode("latin-1").strip().split(" ", 2)
    if len(parts) != 3:
        raise ValueError("Malformed request line")
    
    method, path, version = parts
    
    # Читаем заголовки до пустой строки
    headers = {}
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        # b"Host: example.com\r\n"
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    
    return HttpRequest(method, path, version, headers)