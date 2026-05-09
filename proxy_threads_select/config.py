"""
Конфиг — почти копия proxy/config.py.

Без asyncio.Semaphore: семафоры/локи живут в upstream_pool.py
и создаются на threading.*
"""
from dataclasses import dataclass, field
from typing import List
import yaml


@dataclass
class UpstreamConfig:
    host: str
    port: int

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port}"


@dataclass
class TimeoutConfig:
    """Все значения в миллисекундах; properties отдают секунды."""
    parse_ms: int = 15000           # На парсинг заголовков
    connect_ms: int = 5000          # На non-blocking connect к upstream
    read_ms: int = 15000            # На чтение байтов
    write_ms: int = 15000           # На отправку
    total_ms: int = 30000           # На весь запрос целиком
    keepalive_idle_ms: int = 30000  # Сколько ждём следующий запрос в keep-alive

    @property
    def parse(self) -> float: return self.parse_ms / 1000
    @property
    def connect(self) -> float: return self.connect_ms / 1000
    @property
    def read(self) -> float: return self.read_ms / 1000
    @property
    def write(self) -> float: return self.write_ms / 1000
    @property
    def total(self) -> float: return self.total_ms / 1000
    @property
    def keepalive_idle(self) -> float: return self.keepalive_idle_ms / 1000


@dataclass
class LimitsConfig:
    max_client_conns: int = 1000
    max_conns_per_upstream: int = 250
    backlog: int = 1024


@dataclass
class ProxyConfig:
    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    upstreams: List[UpstreamConfig] = field(default_factory=list)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    log_level: str = "info"

    @classmethod
    def from_yaml(cls, path: str) -> "ProxyConfig":
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        listen = data.get("listen", "127.0.0.1:8080")
        if ":" in listen:
            host, port = listen.rsplit(":", 1)
            listen_host, listen_port = host, int(port)
        else:
            listen_host, listen_port = listen, 8080

        ups = [UpstreamConfig(u["host"], u["port"]) for u in data.get("upstreams", [])]

        t = data.get("timeouts", {})
        timeouts = TimeoutConfig(
            parse_ms=t.get("parse_ms", 15000),
            connect_ms=t.get("connect_ms", 5000),
            read_ms=t.get("read_ms", 15000),
            write_ms=t.get("write_ms", 15000),
            total_ms=t.get("total_ms", 30000),
            keepalive_idle_ms=t.get("keepalive_idle_ms", 30000),
        )

        l = data.get("limits", {})
        limits = LimitsConfig(
            max_client_conns=l.get("max_client_conns", 1000),
            max_conns_per_upstream=l.get("max_conns_per_upstream", 250),
            backlog=l.get("backlog", 1024),
        )

        return cls(
            listen_host=listen_host,
            listen_port=listen_port,
            upstreams=ups,
            timeouts=timeouts,
            limits=limits,
            log_level=data.get("logging", {}).get("level", "info"),
        )

    @classmethod
    def default(cls) -> "ProxyConfig":
        return cls(upstreams=[
            UpstreamConfig("127.0.0.1", 9001),
            UpstreamConfig("127.0.0.1", 9002),
        ])
