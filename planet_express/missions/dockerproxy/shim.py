"""The Core-owned client shim (contract 6.1: "an `X-PE-Session` header injected by a Core-owned shim"). A stock Docker client
cannot add a header of its own, and the session token is delivered on an inherited file descriptor only (never argv, env or a
file). The runtime that holds the token runs this beside the agent: a stdlib-only relay on a socket of its own that puts the
header on the first request of every connection and then passes bytes both ways (exec and attach hijacks included). Clients are
pointed at the shim's socket (`DOCKER_HOST=unix://<shim socket>`); the proxy still checks the peer's uid and cgroup, which a
relay in the session's own cgroup does not change. The shim is not a trust boundary: it adds nothing the session does not hold.

    python3 -I shim.py --listen /path/shim.sock --target /path/proxy.sock --token-fd 3
"""

import argparse
import os
import re
import select
import signal
import socket
import threading
import time

MAX_HEAD = 16 * 1024
HEADER = "X-PE-Session"
_TOKEN = re.compile(r"^[A-Za-z0-9_-]{16,256}$")


class TokenShim:
    def __init__(self, listen_path: str, target_path: str, token: str, *, idle_s: float = 600.0, max_connections: int = 16):
        if not _TOKEN.match(token):
            raise ValueError("not a session token")
        self.listen_path, self.target_path, self.token = listen_path, target_path, token
        self.idle_s = idle_s
        self._slots = threading.BoundedSemaphore(max_connections)
        self._stop = threading.Event()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(self.listen_path)
        finally:
            os.umask(old)
        sock.listen(8)
        sock.settimeout(0.2)
        self._sock = sock
        self._thread = threading.Thread(target=self._accept, daemon=True, name="pe-docker-shim")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            self._sock.close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            os.unlink(self.listen_path)
        except OSError:
            pass

    def _accept(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, client: socket.socket) -> None:
        up = None
        try:
            head, rest = self._read_head(client)
            if head is None:
                return
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.settimeout(10)
            up.connect(self.target_path)
            up.sendall(self._with_token(head) + rest)
            self._relay(client, up)
        except OSError:
            pass
        finally:
            client.close()
            if up is not None:
                up.close()
            self._slots.release()

    def _read_head(self, client: socket.socket) -> tuple[bytes | None, bytes]:
        client.settimeout(10)
        buf = b""
        while b"\r\n\r\n" not in buf:
            if len(buf) > MAX_HEAD:
                return None, b""
            chunk = client.recv(4096)
            if not chunk:
                return None, b""
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head, b"\r\n\r\n" + rest

    def _with_token(self, head: bytes) -> bytes:
        """The request head with exactly one session header, ours, straight after the request line."""
        first, _, others = head.partition(b"\r\n")
        kept = [line for line in others.split(b"\r\n") if line and line.split(b":", 1)[0].strip().lower() != HEADER.lower().encode()]
        return b"\r\n".join([first, f"{HEADER}: {self.token}".encode("ascii"), *kept])

    def _relay(self, a: socket.socket, b: socket.socket) -> None:
        sources = [a, b]
        last = time.monotonic()
        while sources and not self._stop.is_set() and time.monotonic() - last < self.idle_s:
            ready, _, _ = select.select(sources, [], [], 1.0)
            for src in ready:
                data = src.recv(65536)
                dest = b if src is a else a
                if not data:
                    sources.remove(src)
                    try:
                        dest.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    if src is b:                     # the daemon side is done: nothing more will come back
                        return
                    continue
                last = time.monotonic()
                dest.sendall(data)


def main(argv: list[str] | None = None) -> int:           # pragma: no cover -- a thin CLI over TokenShim
    parser = argparse.ArgumentParser(description="Inject the session token into Docker client requests for the proxy socket.")
    parser.add_argument("--listen", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--token-fd", type=int, required=True, help="an inherited fd holding the token (read once, then closed)")
    args = parser.parse_args(argv)
    token = os.read(args.token_fd, 512).decode("ascii", "replace").strip()
    os.close(args.token_fd)
    shim = TokenShim(args.listen, args.target, token)
    shim.start()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    stop.wait()
    shim.stop()
    return 0


if __name__ == "__main__":           # pragma: no cover
    raise SystemExit(main())


__all__ = ["TokenShim", "main"]
