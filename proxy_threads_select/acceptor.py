"""
Acceptor: один поток, accept() на listening-сокете и
round-robin раздача новых клиентов по reactor-ам.

Использует selectors на самом listening-сокете, чтобы на остановку
проснуться через wakeup-pair и красиво выйти.
"""
import itertools
import selectors
import socket
import threading

from .logger import get_logger

log = get_logger()


class Acceptor(threading.Thread):
    def __init__(self, lsock: socket.socket, reactors: list):
        super().__init__(daemon=True, name="acceptor")
        self.lsock = lsock
        self.reactors = reactors
        self._rr = itertools.cycle(range(len(reactors)))
        self._stopping = False

        self.sel = selectors.DefaultSelector()
        self.sel.register(self.lsock, selectors.EVENT_READ)

        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self.sel.register(self._wake_r, selectors.EVENT_READ)

    def stop(self) -> None:
        self._stopping = True
        try:
            self._wake_w.send(b"x")
        except OSError:
            pass

    def run(self) -> None:
        log.info("acceptor started")
        while not self._stopping:
            try:
                events = self.sel.select(timeout=1.0)
            except OSError:
                continue
            for key, _ in events:
                if key.fileobj is self.lsock:
                    self._do_accept()
                else:
                    try:
                        while self._wake_r.recv(4096):
                            pass
                    except BlockingIOError:
                        pass
        try:
            self.sel.close()
        except Exception:
            pass
        try:
            self._wake_r.close()
            self._wake_w.close()
        except Exception:
            pass
        log.info("acceptor stopped")

    def _do_accept(self) -> None:
        # принимаем все доступные соединения сразу (edge-friendly)
        while True:
            try:
                cli, addr = self.lsock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                log.warning(f"accept error: {e}")
                return
            idx = next(self._rr)
            self.reactors[idx].push_client(cli, addr)
