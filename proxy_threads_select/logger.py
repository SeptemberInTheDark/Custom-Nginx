"""Простой потокобезопасный логгер. Имя потока попадает в формат."""
import logging


def setup_logger(level: str = "info") -> logging.Logger:
    logger = logging.getLogger("proxy")
    logger.setLevel(getattr(logging, level.upper()))
    logger.handlers.clear()
    h = logging.StreamHandler()
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-5s | [%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    h.setFormatter(fmt)
    logger.addHandler(h)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("proxy")
