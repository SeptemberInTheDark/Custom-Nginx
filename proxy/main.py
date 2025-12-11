#!/usr/bin/env python3
"""
Точка входа для асинхронного reverse proxy сервера.

Запуск:
    python -m proxy.main
    python -m proxy.main --config config.yaml
"""
import argparse
import asyncio
import logging
import signal
from pathlib import Path

from proxy.config import ProxyConfig
from proxy.proxy_server import ProxyServer
from proxy.logger import setup_logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Async Reverse Proxy Server",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "-c", "--config",
        type=str,
        default=None,
        help="Path to YAML config file",
    )
    parser.add_argument(
        "-H", "--host",
        type=str,
        default="127.0.0.1",
        help="Listen host",
    )
    parser.add_argument(
        "-p", "--port",
        type=int,
        default=8080,
        help="Listen port",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="info",
        choices=["debug", "info", "warning", "error"],
        help="Logging level",
    )
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> ProxyConfig:
    """Загружает конфигурацию из файла или использует дефолтную."""
    if args.config and Path(args.config).exists():
        return ProxyConfig.from_yaml(args.config)

    # Дефолтная конфигурация
    config = ProxyConfig.default()
    config.listen_host = args.host
    config.listen_port = args.port
    config.log_level = args.log_level
    return config


async def shutdown(server: ProxyServer, sig: signal.Signals) -> None:
    """Graceful shutdown при получении сигнала."""
    logging.info(f"Received {sig.name}, shutting down...")
    await server.stop()

    # Отменяем все активные задачи
    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    for task in tasks:
        task.cancel()

    await asyncio.gather(*tasks, return_exceptions=True)


async def main() -> None:
    args = parse_args()

    # Настройка логирования
    setup_logger(args.log_level)
    logger = logging.getLogger("proxy")

    # Загрузка конфигурации
    config = load_config(args)
    logger.debug(f"Config loaded: {config}")

    # Создание и запуск сервера
    server = ProxyServer(config)

    # Настройка graceful shutdown
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(shutdown(server, s)),
        )

    try:
        await server.start()
    except asyncio.CancelledError:
        logger.info("Server shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

