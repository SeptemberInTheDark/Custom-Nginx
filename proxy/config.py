"""
Конфигурация прокси-сервера.

Все настройки описаны как dataclasses — это проще Pydantic
и не тянет лишние зависимости.
"""

from dataclasses import dataclass, field
from typing import List
import yaml

from proxy.upstream_pool import Upstream


@dataclass
class UpstreamConfig:
    """
    Один upstream-сервер.

    Используется только для хранения конфига,
    рабочий Upstream с семафором создаётся в upstream_pool.py
    """

    host: str
    port: int

    @property
    def address(self) -> str:
        """Человекочитаемый адрес для логов."""
        return f"{self.host}:{self.port}"


@dataclass
class TimeoutConfig:
    """
    Таймауты для различных операций.

    Храним в миллисекундах (так удобнее в конфиге),
    но properties возвращают секунды для asyncio.wait_for()
    """

    parse_ms: int = 15000  # На парсинг заголовков (keep-alive может быть медленнее)
    connect_ms: int = 5000  # На подключение к upstream
    read_ms: int = 15000  # На стриминг ответа
    write_ms: int = 15000  # На отправку
    total_ms: int = 30000  # Общий таймаут на весь запрос

    @property
    def parse(self) -> float:
        return self.parse_ms / 1000

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
    """Лимиты на количество соединений."""

    max_client_conns: int = 1000  # сколько клиентов держим одновременно
    max_conns_per_upstream: int = 100  # чтобы не завалить upstream
    backlog: int = 32768  # очередь входящих соединений
    limit: int = 1048576  # max buffer size


@dataclass
class ProxyConfig:
    """
    Корневой конфиг приложения.

    Можно создать через from_yaml() или default() для разработки.
    """

    listen_host: str = "127.0.0.1"
    listen_port: int = 8080
    upstreams: List[UpstreamConfig] = field(default_factory=list)
    timeouts: TimeoutConfig = field(default_factory=TimeoutConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    log_level: str = "info"

    @classmethod
    def from_yaml(cls, path: str) -> "ProxyConfig":
        """
        Парсит YAML-конфиг.

        Формат см. в config.example.yaml
        """
        with open(path, "r") as f:
            data = yaml.safe_load(f)

        # listen может быть "127.0.0.1:8080" или просто "0.0.0.0"
        listen = data.get("listen", "127.0.0.1:8080")
        if ":" in listen:
            host, port = listen.rsplit(":", 1)  # rsplit на случай IPv6
            listen_host = host
            listen_port = int(port)
        else:
            listen_host = listen
            listen_port = 8080

        # Сначала парсим лимиты, потом используем их для upstreams
        limits_data = data.get("limits", {})
        limits = LimitsConfig(
            max_client_conns=limits_data.get("max_client_conns", 1000),
            max_conns_per_upstream=limits_data.get("max_conns_per_upstream", 100),
            backlog=limits_data.get("backlog", 32768),
            limit=limits_data.get("limit", 1048576),
        )

        # Теперь создаем upstreams с правильным max_connections из конфига
        upstreams_config = data.get("upstreams", [])
        upstreams_list = []
        for u in upstreams_config:
            upstream = Upstream(
                host=u["host"],
                port=u["port"],
                max_connections=limits.max_conns_per_upstream,  # ← ИСПРАВЛЕНО!
            )
            upstreams_list.append(upstream)

        # Парсим таймауты
        timeouts_data = data.get("timeouts", {})
        timeouts = TimeoutConfig(
            parse_ms=timeouts_data.get("parse_ms", 15000),
            connect_ms=timeouts_data.get("connect_ms", 5000),
            read_ms=timeouts_data.get("read_ms", 15000),
            write_ms=timeouts_data.get("write_ms", 15000),
            total_ms=timeouts_data.get("total_ms", 30000),
        )

        return cls(
            listen_host=listen_host,
            listen_port=listen_port,
            upstreams=upstreams_list,
            timeouts=timeouts,
            limits=limits,
            log_level=data.get("logging", {}).get("level", "info"),
        )

    @classmethod
    def default(cls) -> "ProxyConfig":
        """Дефолтный конфиг для локальной разработки."""
        upstreams = [
            Upstream(host="127.0.0.1", port=9001, max_connections=100),
            Upstream(host="127.0.0.1", port=9002, max_connections=100),
        ]
        return cls(upstreams=upstreams)
