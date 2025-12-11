import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass

@dataclass
class RequestLog:
    method: str
    path: str
    upstream: str
    status: int
    duration_ms: float
    bytes_sent: int = 0
    error: str = ""


def setup_logger(level: str = "info") -> logging.Logger:
    logger = logging.getLogger("proxy")
    logger.setLevel(getattr(logging, level.upper()))

    handler = logging.StreamHandler()
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


@contextmanager
def log_request(logger: logging.Logger, method: str, path: str):
    """Контекст для измерения времени запроса"""
    start = time.perf_counter()
    log = RequestLog(method=method, path=path, upstream="", status=0, duration_ms=0)

    try:
        yield log
    finally:
        log.duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            f"{log.method} {log.path} -> {log.upstream} | "
            f"{log.status} | {log.duration_ms:.2f}ms"
        )