"""
Настройка логирования.

Ничего особенного — стандартный logging с форматом под консоль.
"""
import logging
import time
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass
class RequestLog:
    """
    Данные для лога запроса.
    
    Заполняется по ходу обработки и выводится в finally.
    """
    method: str
    path: str
    upstream: str
    status: int
    duration_ms: float
    bytes_sent: int = 0
    error: str = ""


def setup_logger(level: str = "info") -> logging.Logger:
    """
    Настраивает логгер "proxy".
    
    Формат: 2025-01-15 12:30:45 | INFO | message
    """
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
    """
    Контекст для измерения времени запроса.
    
    Использование:
        with log_request(logger, "GET", "/api") as log:
            log.upstream = "127.0.0.1:9001"
            log.status = 200
        # автоматически залогирует с duration
    """
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
