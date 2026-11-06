"""A small userspace TCP proxy that makes a link behave like unreliable Wi-Fi (COORD-003).

Standard library only. The proxy forwards bytes between a client and one upstream address and
can, while running:

* add latency and seeded jitter to every forwarded chunk (``set_delay``);
* throttle each direction to a byte rate (``set_throttle``);
* reset connections: ``SO_LINGER`` with a zero timeout makes ``close`` send an RST, which is what a
  client sees when an access point drops the association (``reset_all``);
* stall: stop forwarding in one or both directions without closing anything, which is how a
  half-open connection looks to both ends (``stall``); and
* refuse new connections (``refuse``), so a worker's reconnect attempts fail until ``allow``.

All randomness comes from one ``random.Random(seed)``. TLS passes through untouched, so the client
must trust the real server's certificate for the name it dials.
"""

from __future__ import annotations

import random
import select
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

UP = "up"  # client to upstream (worker to coordinator)
DOWN = "down"  # upstream to client (coordinator to worker)
CHUNK = 16 * 1024


@dataclass
class _Pair:
    client: socket.socket
    upstream: socket.socket
    closed: threading.Event = field(default_factory=threading.Event)


class FaultProxy:
    def __init__(self, upstream_port: int, *, seed: int = 0, host: str = "127.0.0.1") -> None:
        self.upstream = (host, upstream_port)
        self.random = random.Random(seed)
        self._random_lock = threading.Lock()
        self._lock = threading.Lock()
        self._pairs: list[_Pair] = []
        self._latency = 0.0
        self._jitter = 0.0
        self._throttle: dict[str, float] = {}
        self._stalled: set[str] = set()
        self._refusing = False
        self._stop = threading.Event()
        self.accepted = 0
        self.refused = 0
        self.resets = 0
        self.forwarded = {UP: 0, DOWN: 0}
        self._listener = socket.socket()
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind((host, 0))
        self._listener.listen(64)
        self._listener.settimeout(0.1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    # ---- fault controls
    def set_delay(self, latency: float = 0.0, jitter: float = 0.0) -> None:
        """Every chunk waits ``latency`` plus a seeded random amount up to ``jitter`` seconds."""
        self._latency, self._jitter = latency, jitter

    def set_throttle(self, up_bytes_per_second: float | None = None, down_bytes_per_second: float | None = None) -> None:
        self._throttle = {key: rate for key, rate in ((UP, up_bytes_per_second), (DOWN, down_bytes_per_second)) if rate}

    def stall(self, *directions: str) -> None:
        """Stop forwarding in these directions (both when none are given); sockets stay open. New connections are stalled too."""
        self._stalled = set(directions or (UP, DOWN))

    def unstall(self) -> None:
        self._stalled = set()

    def refuse(self) -> None:
        """Close new connections at once; existing ones are untouched."""
        self._refusing = True

    def allow(self) -> None:
        self._refusing = False

    def reset_all(self) -> int:
        """Send an RST on every connection now, dropping any bytes the proxy still holds. Returns how many were reset."""
        with self._lock:
            pairs, self._pairs = self._pairs, []
        for pair in pairs:
            self._close(pair, reset=True)
        self.resets += len(pairs)
        return len(pairs)

    def cut(self) -> None:
        """The link is gone: refuse new connections and reset the existing ones."""
        self.refuse()
        self.reset_all()

    def restore(self) -> None:
        """Back to a clean link: no stall, no refusal (delay and throttle are left as set)."""
        self.unstall()
        self.allow()

    def active(self) -> int:
        with self._lock:
            return sum(1 for pair in self._pairs if not pair.closed.is_set())

    def close(self) -> None:
        self._stop.set()
        self._thread.join(5)
        self._listener.close()
        self.reset_all()

    # ---- internals
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                client, _ = self._listener.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                return
            if self._refusing:
                self.refused += 1
                self._abort(client)
                continue
            try:
                upstream = socket.create_connection(self.upstream, timeout=5)
            except OSError:
                self._abort(client)
                continue
            for sock in (client, upstream):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            pair = _Pair(client, upstream)
            with self._lock:
                self._pairs.append(pair)
            self.accepted += 1
            threading.Thread(target=self._pump, args=(pair, client, upstream, UP), daemon=True).start()
            threading.Thread(target=self._pump, args=(pair, upstream, client, DOWN), daemon=True).start()

    @staticmethod
    def _abort(sock: socket.socket) -> None:
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        except OSError:
            pass
        sock.close()

    def _close(self, pair: _Pair, *, reset: bool) -> None:
        if pair.closed.is_set():
            return
        pair.closed.set()
        for sock in (pair.client, pair.upstream):
            if reset:
                self._abort(sock)
            else:
                try:
                    sock.close()
                except OSError:
                    pass

    def _pump(self, pair: _Pair, source: socket.socket, sink: socket.socket, direction: str) -> None:
        try:
            while not pair.closed.is_set() and not self._stop.is_set():
                if direction in self._stalled:
                    time.sleep(0.01)  # hold: unread bytes queue in the kernel, nothing is closed
                    continue
                ready, _, _ = select.select([source], [], [], 0.05)
                if not ready:
                    continue
                if direction in self._stalled:
                    continue
                data = source.recv(CHUNK)
                if not data:
                    break
                delay = self._latency
                if self._jitter:
                    with self._random_lock:
                        delay += self.random.uniform(0.0, self._jitter)
                if delay:
                    time.sleep(delay)
                if pair.closed.is_set():
                    break
                sink.sendall(data)
                self.forwarded[direction] += len(data)
                rate = self._throttle.get(direction)
                if rate:
                    time.sleep(len(data) / rate)
        except OSError:
            pass
        finally:
            with self._lock:
                if pair in self._pairs:
                    self._pairs.remove(pair)
            self._close(pair, reset=False)
