import asyncio
from dataclasses import dataclass, field
from typing import Dict

@dataclass
class Metrics:
    total_requests: int = 0
    active_connections: int = 0
    requests_by_status: Dict[int, int] = field(default_factory=dict)
    requests_by_upstream: Dict[str, int] = field(default_factory=dict)
    total_bytes_in: int = 0
    total_bytes_out: int = 0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)


    async def inc_request(self, status: int, upstream: str):
        async with self._lock:
            self.total_requests += 1
            self.requests_by_status[status] = self.requests_by_status.get(status, 0) + 1
            self.requests_by_upstream[upstream] = self.requests_by_upstream.get(upstream, 0) + 1