"""
Тело worker-процесса.

Каждый worker:
- получает inherited fd listening-сокета от мастера (fork);
- крутит свой accept-loop в главном потоке;
- отдаёт каждое принятое соединение в собственный ThreadPoolExecutor;
- держит свой UpstreamPool (idle-сокеты не шарятся между процессами).

Глобальный лимит клиентов (max_client_conns) — на воркер.
"""
import os
import signal
import socket
import threading
from concurrent.futures import ThreadPoolExecutor

from .config import ProxyConfig
from .handler import handle_client
from .logger import get_logger, setup_logger
from .upstream_pool import UpstreamPool


def worker_main(idx: int, listen_fd: int, cfg: ProxyConfig, log_level: str) -> None:
    """Точка входа дочернего процесса."""
    setup_logger(log_level, role=f"worker-{idx}")
    log = get_logger()
    log.info(f"worker {idx} started (pid={os.getpid()})")

    # пересобираем listening-сокет из inherited fd
    lsock = socket.fromfd(listen_fd, socket.AF_INET, socket.SOCK_STREAM)
    # fromfd дублирует fd; оригинал у нас не нужен (мастер закроет свой)
    os.close(listen_fd)

    pool = UpstreamPool(cfg.upstreams, cfg.limits.max_conns_per_upstream)
    sema = threading.BoundedSemaphore(cfg.limits.max_client_conns)
    executor = ThreadPoolExecutor(
        max_workers=cfg.limits.max_client_conns,
        thread_name_prefix=f"w{idx}",
    )

    stop_evt = threading.Event()

    def handle_sig(_sig, _frame):
        log.info(f"worker {idx} got signal, draining")
        stop_evt.set()
        try:
            # выводим accept() из блока: shutdown(SHUT_RD) даст OSError
            lsock.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    # accept-loop
    while not stop_evt.is_set():
        try:
            cli, addr = lsock.accept()
        except OSError as e:
            if stop_evt.is_set():
                break
            log.warning(f"accept error: {e}")
            continue

        # глобальный per-worker лимит
        if not sema.acquire(blocking=False):
            _reject_503(cli)
            continue

        try:
            executor.submit(_run_one, cli, addr, pool, cfg.timeouts, sema)
        except RuntimeError:
            # executor уже остановлен
            sema.release()
            try:
                cli.close()
            except Exception:
                pass

    log.info(f"worker {idx} stopping accept-loop, waiting threads")
    executor.shutdown(wait=True)
    pool.close_all_idle()
    try:
        lsock.close()
    except Exception:
        pass
    log.info(f"worker {idx} stopped")


def _run_one(client: socket.socket, addr, pool, timeouts, sema) -> None:
    try:
        handle_client(client, pool, timeouts)
    except Exception as e:
        get_logger().exception(f"handler crashed: {e}")
    finally:
        try:
            client.close()
        except Exception:
            pass
        try:
            sema.release()
        except ValueError:
            pass


def _reject_503(sock: socket.socket) -> None:
    try:
        sock.sendall(
            b"HTTP/1.1 503 Service Unavailable\r\n"
            b"Content-Length: 0\r\n"
            b"Connection: close\r\n"
            b"\r\n"
        )
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass
