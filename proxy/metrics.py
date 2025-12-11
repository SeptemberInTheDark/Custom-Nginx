"""
Простые счётчики метрик.

Пока не интегрировано — заготовка на будущее.
Можно добавить /metrics эндпоинт с prometheus-форматом.
"""
import asyncio
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Metrics:
    """
    In-memory счётчики.
    
    Lock нужен потому что dict.get() + присвоение не атомарны,
    а мы обновляем из разных корутин.
    """
    total_requests: int = 0
    active_connections: int = 0
    requests_by_status: Dict[int, int] = field(default_factory=dict)
    requests_by_upstream: Dict[str, int] = field(default_factory=dict)
    total_bytes_in: int = 0
    total_bytes_out: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def inc_request(self, status: int, upstream: str) -> None:
        """Инкрементит счётчики после обработки запроса."""
        async with self._lock:
            self.total_requests += 1
            self.requests_by_status[status] = self.requests_by_status.get(status, 0) + 1
            self.requests_by_upstream[upstream] = self.requests_by_upstream.get(upstream, 0) + 1

    async def inc_bytes(self, bytes_in: int, bytes_out: int) -> None:
        """Счётчик трафика."""
        async with self._lock:
            self.total_bytes_in += bytes_in
            self.total_bytes_out += bytes_out

    def snapshot(self) -> dict:
        """
        Снимок метрик для вывода.
        
        Не под локом — может быть слегка неконсистентным,
        но для метрик это ок.
        """
        return {
            "total_requests": self.total_requests,
            "active_connections": self.active_connections,
            "requests_by_status": dict(self.requests_by_status),
            "requests_by_upstream": dict(self.requests_by_upstream),
            "total_bytes_in": self.total_bytes_in,
            "total_bytes_out": self.total_bytes_out,
        }
