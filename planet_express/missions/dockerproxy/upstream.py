"""The proxy's connection to the real Docker daemon (a Unix socket, never TCP), and the read-only questions the policy asks
it. Every call has a timeout and a size cap; any surprise is `UpstreamError`, which the server turns into a refusal."""

import http.client
import json
import re
import socket
from collections.abc import Iterable
from urllib.parse import quote, urlencode

from .policy import ContainerInfo, NetworkInfo

MAX_INSPECT = 4 * 1024 * 1024


class UpstreamError(Exception):
    """The daemon could not be asked, or answered something this proxy will not interpret."""


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("docker", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self._path)
        except OSError:
            sock.close()
            raise
        self.sock = sock


def request_target(version: str | None, path: str, query: dict[str, str]) -> str:
    target = (f"/{version}" if version else "") + path
    return target + ("?" + urlencode(sorted(query.items()), quote_via=quote) if query else "")


class Upstream:
    def __init__(self, socket_path: str, *, timeout: float = 30.0):
        self.socket_path, self.timeout = socket_path, timeout

    def open(self, method: str, target: str, *, body: bytes | Iterable[bytes] | None = None, length: int | None = None,
             content_type: str | None = None, chunked: bool = False) -> tuple[http.client.HTTPConnection, http.client.HTTPResponse]:
        conn = _UnixConnection(self.socket_path, self.timeout)
        headers = {"Host": "docker", "Connection": "close"}
        if content_type:
            headers["Content-Type"] = content_type
        if not chunked and (body is not None or method in ("POST", "PUT")):
            headers["Content-Length"] = str(length if length is not None else len(body or b""))
        try:
            conn.request(method, target, body=body, headers=headers, encode_chunked=chunked)
            return conn, conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise UpstreamError(type(exc).__name__) from None
        except BaseException:                      # a refusal raised while the request body is being read
            conn.close()
            raise

    def api_version(self) -> int | None:
        """The daemon's own API minor version (`1.<minor>`) from its ping, or None if it cannot be read."""
        try:
            _, headers, _ = self.call("GET", "/_ping", cap=4096)
        except UpstreamError:
            return None
        found = re.fullmatch(r"1\.(\d{1,3})", headers.get("api-version", ""))
        return int(found.group(1)) if found else None

    def call(self, method: str, target: str, *, body: bytes | None = None, content_type: str | None = None,
             cap: int = MAX_INSPECT) -> tuple[int, dict[str, str], bytes]:
        conn, resp = self.open(method, target, body=body, content_type=content_type)
        try:
            data = resp.read(cap + 1)
            if len(data) > cap:
                raise UpstreamError("response too large")
            return resp.status, {k.lower(): v for k, v in resp.getheaders()}, data
        except (OSError, http.client.HTTPException) as exc:
            raise UpstreamError(type(exc).__name__) from None
        finally:
            conn.close()

    def _raw(self, request: bytes) -> tuple[socket.socket, bytes, bytes]:
        """Send `request`, read the response head only: (the daemon's socket, the head, bytes already read after it)."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
            sock.sendall(request)
            buf = b""
            while b"\r\n\r\n" not in buf:
                if len(buf) > 16384:
                    raise UpstreamError("response head too large")
                chunk = sock.recv(4096)
                if not chunk:
                    raise UpstreamError("closed before the response head")
                buf += chunk
            head, _, rest = buf.partition(b"\r\n\r\n")
            return sock, head + b"\r\n\r\n", rest
        except UpstreamError:
            sock.close()
            raise
        except OSError as exc:
            sock.close()
            raise UpstreamError(type(exc).__name__) from None

    def stream(self, target: str) -> tuple[socket.socket, bytes, bytes]:
        """A GET whose body is read raw until the daemon closes (HTTP/1.0: no chunked framing to decode)."""
        return self._raw(f"GET {target} HTTP/1.0\r\nHost: docker\r\n\r\n".encode("ascii"))

    def hijack(self, target: str, body: bytes, *, upgrade: bool) -> tuple[socket.socket, bytes, bytes]:
        """A POST the daemon answers with a raw bidirectional stream (exec start, attach)."""
        extra = "Connection: Upgrade\r\nUpgrade: tcp\r\n" if upgrade else "Connection: close\r\n"
        request = (f"POST {target} HTTP/1.1\r\nHost: docker\r\nContent-Type: application/json\r\n"
                   f"Content-Length: {len(body)}\r\n{extra}\r\n").encode("ascii")
        return self._raw(request + body)


def status_of(head: bytes) -> tuple[int, dict[str, str]]:
    """Status code and lower-cased headers of an upstream response head."""
    lines = head.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/") or not parts[1].isdigit():
        raise UpstreamError("bad status line")
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    return int(parts[1]), headers


class DaemonInspector:
    """The policy's questions, answered by the daemon. A 404 is None; any other failure is UpstreamError (fail closed)."""

    def __init__(self, upstream: Upstream):
        self.up = upstream                      # unversioned paths: the daemon answers in its own current version

    def _get(self, path: str) -> dict | None:
        status, _, data = self.up.call("GET", path)
        if status == 404:
            return None
        if status != 200:
            raise UpstreamError(f"inspect status {status}")
        try:
            doc = json.loads(data.decode("utf-8"))
        except ValueError:
            raise UpstreamError("inspect returned malformed JSON") from None
        if not isinstance(doc, dict):
            raise UpstreamError("inspect returned a non-object")
        return doc

    @staticmethod
    def _labels(value) -> dict[str, str]:
        return {str(k): str(v) for k, v in value.items()} if isinstance(value, dict) else {}

    def container(self, ref: str) -> ContainerInfo | None:
        doc = self._get(f"/containers/{quote(ref, safe='')}/json")
        if doc is None:
            return None
        cid = doc.get("Id")
        if not isinstance(cid, str) or len(cid) != 64:
            raise UpstreamError("inspect returned no container id")
        host, mounts = doc.get("HostConfig"), doc.get("Mounts")
        return ContainerInfo(cid, str(doc.get("Name", "")).lstrip("/"), self._labels((doc.get("Config") or {}).get("Labels")),
                             host if isinstance(host, dict) else {}, tuple(mounts) if isinstance(mounts, list) else ())

    def network(self, ref: str) -> NetworkInfo | None:
        doc = self._get(f"/networks/{quote(ref, safe='')}")
        if doc is None:
            return None
        nid = doc.get("Id")
        if not isinstance(nid, str) or not nid:
            raise UpstreamError("inspect returned no network id")
        return NetworkInfo(nid, str(doc.get("Name", "")), self._labels(doc.get("Labels")))

    def volume(self, name: str) -> dict | None:
        return self._get(f"/volumes/{quote(name, safe='')}")

    def image_exists(self, ref: str) -> bool:
        return self._get(f"/images/{quote(ref, safe='/:@')}/json") is not None


__all__ = ["DaemonInspector", "Upstream", "UpstreamError", "request_target", "status_of"]
