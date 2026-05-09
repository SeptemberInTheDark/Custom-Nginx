"""Логгер с ролью (master/worker-N) и pid в формате."""
import logging
import os


def setup_logger(level: str = "info", role: str = "proc") -> logging.Logger:
    logger = logging.getLogger("proxy")
    logger.setLevel(getattr(logging, level.upper()))
    logger.handlers.clear()
    h = logging.StreamHandler()
    pid = os.getpid()
    fmt = logging.Formatter(
        f"%(asctime)s | %(levelname)-5s | [{role}/{pid}/%(threadName)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    h.setFormatter(fmt)
    logger.addHandler(h)
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger("proxy")
