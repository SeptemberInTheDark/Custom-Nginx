"""
Настройка логирования с поддержкой trace_id.

Каждый запрос получает уникальный trace_id, который автоматически
добавляется во все логи через ContextVar + Filter.
"""
import logging
import time
import uuid
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Optional

# trace_id хранится в contextvars — доступен из любой корутины
# в рамках одного запроса без явной передачи
trace_id_var: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)


def generate_trace_id() -> str:
    """
    Генерирует короткий trace_id.
    
    Берём первые 8 символов UUID — достаточно для отладки,
    не захламляет логи. Коллизии возможны, но для логов не критично.
    """
    return uuid.uuid4().hex[:8]


def get_trace_id() -> Optional[str]:
    """Текущий trace_id или None."""
    return trace_id_var.get()


def set_trace_id(trace_id: str) -> None:
    """Устанавливает trace_id для текущего контекста."""
    trace_id_var.set(trace_id)


class TraceIdFilter(logging.Filter):
    """
    Добавляет trace_id в каждую запись лога.
    
    Если trace_id не установлен — ставит "-".
    """
    def filter(self, record: logging.LogRecord) -> bool:
        record.trace_id = trace_id_var.get() or "-"
        return True


@dataclass
class RequestLog:
    """
    Данные для лога запроса.
    
    Заполняется по ходу обработки и выводится в finally.
    """
    trace_id: str
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
    
    Формат: 2025-01-15 12:30:45 | INFO | [abc12345] message
    """
    logger = logging.getLogger("proxy")
    logger.setLevel(getattr(logging, level.upper()))

    # чистим старые хэндлеры если есть (при перезапуске)
    logger.handlers.clear()

    handler = logging.StreamHandler()
    
    # trace_id в квадратных скобках перед сообщением
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | [%(trace_id)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    handler.setFormatter(formatter)
    handler.addFilter(TraceIdFilter())
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
    trace_id = get_trace_id() or "-"
    start = time.perf_counter()
    log = RequestLog(
        trace_id=trace_id,
        method=method,
        path=path,
        upstream="",
        status=0,
        duration_ms=0
    )

    try:
        yield log
    finally:
        log.duration_ms = (time.perf_counter() - start) * 1000
        logger.info(
            f"{log.method} {log.path} -> {log.upstream} | "
            f"{log.status} | {log.duration_ms:.2f}ms"
        )
