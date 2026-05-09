#!/usr/bin/env python3
"""
Точка входа.

Архитектура:
- 1 acceptor-поток с selectors на listening-сокете;
- N reactor-потоков, у каждого свой selectors.DefaultSelector
  и свой набор сессий;
- общий UpstreamPool (thread-safe) на все reactor'ы.

Запуск:
    python -m proxy_threads_select.main --config config.yaml
    python -m proxy_threads_select.main -p 8080 --workers 4
"""
import argparse
import signal
import socket
import threading

from .acceptor import Acceptor
from .config import ProxyConfig
from .logger import get_logger, setup_logger
from .reactor import Reactor
from .upstream_pool import UpstreamPool


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reverse proxy на потоках + selectors",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-c", "--config", default=None, help="YAML config")
    p.add_argument("-H", "--host", default="127.0.0.1")
    p.add_argument("-p", "--port", type=int, default=8080)
    p.add_argument("--workers", type=int, default=4, help="reactor-потоков")
    p.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    return p.parse_args()


def load_config(args: argparse.Namespace) -> ProxyConfig:
    if args.config:
        cfg = ProxyConfig.from_yaml(args.config)
    else:
        cfg = ProxyConfig.default()
        cfg.listen_host = args.host
        cfg.listen_port = args.port
    return cfg


def make_listening_socket(cfg: ProxyConfig) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind((cfg.listen_host, cfg.listen_port))
    s.listen(cfg.limits.backlog)
    s.setblocking(False)
    return s


def main() -> None:
    args = parse_args()
    setup_logger(args.log_level)
    log = get_logger()

    cfg = load_config(args)
    pool = UpstreamPool(cfg.upstreams, cfg.limits.max_conns_per_upstream)
    lsock = make_listening_socket(cfg)

    reactors = [Reactor(i, pool, cfg.timeouts) for i in range(args.workers)]
    for r in reactors:
        r.start()

    acceptor = Acceptor(lsock, reactors)
    acceptor.start()

    log.info(
        f"started on {cfg.listen_host}:{cfg.listen_port} "
        f"with {args.workers} reactors, "
        f"upstreams={[u.address for u in cfg.upstreams]}"
    )

    stop_evt = threading.Event()

    def handle_signal(sig, _frame):
        log.info(f"got signal {sig}, shutting down")
        stop_evt.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    stop_evt.wait()

    acceptor.stop()
    for r in reactors:
        r.stop()
    acceptor.join(timeout=5)
    for r in reactors:
        r.join(timeout=5)
    try:
        lsock.close()
    except Exception:
        pass
    log.info("stopped")


if __name__ == "__main__":
    main()
