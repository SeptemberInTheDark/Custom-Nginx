from dataclasses import dataclass, field
from typing import List, Optional
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
    connect_ms: int = 1000
    read_ms: int = 15000
    write_ms: int = 15000
    total_ms: int = 30000

    @property
    def connect(self) -> float:
        return self.connect_ms / 1000

    @property
    def read(self) -> float:
        return self.read_ms / 1000

    @property
    def write(self) -> float:
        return self.write_ms / 1000

    @property
    def total(self) -> float:
        return self.total_ms / 1000


@dataclass
class LimitsConfig:
    max_client_conns: int = 1000
    max_conns_per_upstream: int = 100


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

        # Парсим listen адрес "127.0.0.1:8080"
        listen = data.get("listen", "127.0.0.1:8080")
        if ":" in listen:
            host, port = listen.rsplit(":", 1)
            listen_host = host
            listen_port = int(port)
        else:
            listen_host = listen
            listen_port = 8080

        # Парсим upstreams
        upstreams = [
            UpstreamConfig(host=u["host"], port=u["port"])
            for u in data.get("upstreams", [])
        ]

        # Парсим timeouts
        timeouts_data = data.get("timeouts", {})
        timeouts = TimeoutConfig(
            connect_ms=timeouts_data.get("connect_ms", 1000),
            read_ms=timeouts_data.get("read_ms", 15000),
            write_ms=timeouts_data.get("write_ms", 15000),
            total_ms=timeouts_data.get("total_ms", 30000),
        )

        # Парсим limits
        limits_data = data.get("limits", {})
        limits = LimitsConfig(
            max_client_conns=limits_data.get("max_client_conns", 1000),
            max_conns_per_upstream=limits_data.get("max_conns_per_upstream", 100),
        )

        return cls(
            listen_host=listen_host,
            listen_port=listen_port,
            upstreams=upstreams,
            timeouts=timeouts,
            limits=limits,
            log_level=data.get("logging", {}).get("level", "info"),
        )

    @classmethod
    def default(cls) -> "ProxyConfig":
        """Конфигурация по умолчанию для разработки."""
        return cls(
            upstreams=[
                UpstreamConfig(host="127.0.0.1", port=9001),
                UpstreamConfig(host="127.0.0.1", port=9002),
            ]
        )
