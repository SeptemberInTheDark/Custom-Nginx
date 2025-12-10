from dataclasses import dataclass, field
from typing import List
import yaml


@dataclass
class UpstreamConfig:
    host: str
    url: str

    @property
    def addres(self) -> str:
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
        return cls(**data)
        