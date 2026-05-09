#!/usr/bin/env python3
"""
Master-процесс.

Что делает:
- биндит listening-сокет;
- форкает N worker-процессов (multiprocessing с force=fork —
  на Linux дешевле и шарит fd; на macOS нужно явно set_start_method);
- следит за воркерами; респавнит упавших, пока не получили SIGINT/SIGTERM;
- по сигналу пробрасывает SIGTERM воркерам, ждёт их завершения и выходит.

Запуск:
    python -m proxy_processes.main --config config.yaml --workers 4
    python -m proxy_processes.main -p 8080 --workers 2
"""
import argparse
import multiprocessing as mp
import os
import signal
import socket
import time

from .config import ProxyConfig
from .logger import get_logger, setup_logger
from .worker import worker_main


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Reverse proxy в pre-fork процессной модели",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("-c", "--config", default=None)
    p.add_argument("-H", "--host", default="127.0.0.1")
    p.add_argument("-p", "--port", type=int, default=8080)
    p.add_argument(
        "--workers",
        type=int,
        default=os.cpu_count() or 2,
        help="число worker-процессов",
    )
    p.add_argument(
        "--log-level",
        default="info",
        choices=["debug", "info", "warning", "error"],
    )
    p.add_argument(
        "--no-respawn",
        action="store_true",
        help="не перезапускать упавших воркеров",
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
    # blocking — воркер сам выставит non-blocking если нужно
    return s


def spawn_worker(idx: int, lsock: socket.socket, cfg: ProxyConfig, log_level: str):
    """Форкаем нового воркера, передаём ему дублированный fd."""
    # Process(target=...) с fork-методом унаследует все open fd,
    # но мы аккуратнее: дублируем fd и передаём номер аргументом,
    # чтобы worker_main мог сам управлять временем жизни.
    fd = os.dup(lsock.fileno())
    p = mp.Process(
        target=worker_main,
        args=(idx, fd, cfg, log_level),
        name=f"worker-{idx}",
        daemon=False,
    )
    p.start()
    # после старта дочернего процесса мастеру эта копия fd не нужна:
    # дочерний унаследовал и тоже её получил через fromfd.
    os.close(fd)
    return p


def main() -> None:
    # на macOS дефолт — spawn; нам нужен fork, чтобы дочерние
    # унаследовали listening-сокет и могли его пересобрать через fromfd.
    try:
        mp.set_start_method("fork")
    except RuntimeError:
        # уже выставлен в этом процессе
        pass

    args = parse_args()
    setup_logger(args.log_level, role="master")
    log = get_logger()

    cfg = load_config(args)
    lsock = make_listening_socket(cfg)

    log.info(
        f"master pid={os.getpid()} listening on "
        f"{cfg.listen_host}:{cfg.listen_port}, "
        f"forking {args.workers} workers, "
        f"upstreams={[u.address for u in cfg.upstreams]}"
    )

    workers = [spawn_worker(i, lsock, cfg, args.log_level) for i in range(args.workers)]

    stop = {"flag": False}

    def handle_sig(sig, _frame):
        if stop["flag"]:
            return
        log.info(f"master got signal {sig}, propagating to workers")
        stop["flag"] = True
        for w in workers:
            try:
                os.kill(w.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    # супервизорный цикл
    while not stop["flag"]:
        time.sleep(1.0)
        if args.no_respawn:
            continue
        for i, w in enumerate(list(workers)):
            if not w.is_alive() and not stop["flag"]:
                log.warning(
                    f"worker {i} (pid={w.pid}) died exitcode={w.exitcode}, respawning"
                )
                workers[i] = spawn_worker(i, lsock, cfg, args.log_level)

    # graceful shutdown
    deadline = time.monotonic() + 10
    for w in workers:
        remaining = max(0.1, deadline - time.monotonic())
        w.join(timeout=remaining)
        if w.is_alive():
            log.warning(f"worker {w.pid} still alive, sending SIGKILL")
            try:
                os.kill(w.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            w.join()

    try:
        lsock.close()
    except Exception:
        pass
    log.info("master stopped")


if __name__ == "__main__":
    main()
