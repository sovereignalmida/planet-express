"""Strict HTTP/1.1 reading for the Docker proxy. One request per connection, no keep-alive, no pipelining, no chunked
bodies, no header folding: anything ambiguous is refused, never guessed at. The proxy never forwards the client's bytes; it
rebuilds the upstream request from what it parsed and validated here, so a framing trick has nothing to smuggle into."""

import json
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

MAX_HEAD = 16 * 1024
MAX_HEADERS = 48
MAX_TARGET = 2048
MAX_JSON_DEPTH = 8
METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_VALUE = re.compile(r"^[\x20-\x7e\t]*$")
_VERSION = re.compile(r"^v1\.(\d{2})$")
_LENGTH = re.compile(r"^\d{1,10}$")


class Refusal(Exception):
    """The request is not served. `reason` is a short fixed phrase (never client text); `status` is what the client sees."""

    def __init__(self, reason: str, status: int = 403):
        super().__init__(reason)
        self.reason, self.status = reason, status


class BadRequest(Refusal):
    def __init__(self, reason: str):
        super().__init__(reason, 400)


@dataclass
class Head:
    method: str
    path: str                       # normalised, no query
    segments: tuple[str, ...]
    query: dict[str, str]
    headers: dict[str, str]         # lower-cased names
    version: str | None             # "v1.43" or None for an unversioned path
    leftover: bytes                 # body bytes that arrived with the head
    chunked: bool = False           # `Transfer-Encoding: chunked` and nothing else (never together with Content-Length)


def _deadline_recv(conn: socket.socket, n: int, deadline: float) -> bytes:
    left = deadline - time.monotonic()
    if left <= 0:
        raise BadRequest("request timed out")
    conn.settimeout(left)
    try:
        return conn.recv(n)
    except (TimeoutError, OSError):
        raise BadRequest("request timed out") from None


def read_head(conn: socket.socket, *, timeout: float) -> Head:
    """Read and validate the request line and headers. Raises Refusal on anything that is not plainly well formed."""
    deadline = time.monotonic() + timeout
    buf = b""
    while b"\r\n\r\n" not in buf:
        if len(buf) > MAX_HEAD:
            raise BadRequest("request head too large")
        chunk = _deadline_recv(conn, 4096, deadline)
        if not chunk:
            raise BadRequest("connection closed")
        buf += chunk
    raw, _, leftover = buf.partition(b"\r\n\r\n")
    if len(raw) > MAX_HEAD:
        raise BadRequest("request head too large")
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        raise BadRequest("non-ascii request head") from None
    bare = text.replace("\r\n", "")
    if "\r" in bare or "\n" in bare or "\x00" in text:
        raise BadRequest("bare CR, LF or NUL in the request head")
    lines = text.split("\r\n")
    request = lines[0].split(" ")
    if len(request) != 3 or request[2] != "HTTP/1.1" or request[0] not in METHODS:
        raise BadRequest("unsupported request line")
    if len(lines) - 1 > MAX_HEADERS:
        raise BadRequest("too many headers")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or line[0] in " \t":
            raise BadRequest("empty or folded header")
        name, sep, value = line.partition(":")
        if not sep or not _TOKEN.match(name) or not _VALUE.match(value):
            raise BadRequest("malformed header")
        key = name.lower()
        if key in headers:
            raise BadRequest("duplicate header")           # includes a second Content-Length
        headers[key] = value.strip()
    chunked = False
    if "transfer-encoding" in headers:
        if headers["transfer-encoding"].lower() != "chunked" or "content-length" in headers:
            raise BadRequest("Transfer-Encoding is not accepted")       # includes Content-Length with Transfer-Encoding
        chunked = True
    if "expect" in headers:
        raise BadRequest("Expect is not accepted")
    path, segments, query, version = normalise_target(request[1])
    return Head(request[0], path, segments, query, headers, version, leftover, chunked)


def normalise_target(target: str) -> tuple[str, tuple[str, ...], dict[str, str], str | None]:
    """Path and query of a request target, or Refusal. No encoded path characters, no dot segments, no empty segments,
    no backslash, no fragment, no NUL; the query is parsed strictly and may not repeat a key."""
    if not target.startswith("/") or len(target) > MAX_TARGET or not target.isascii():
        raise BadRequest("bad request target")
    if any(c in target for c in ("\\", "#", "\x00")) or any(ord(c) < 0x21 for c in target):
        raise BadRequest("bad character in request target")
    path, _, qs = target.partition("?")
    if "%" in path or ";" in path:
        raise BadRequest("encoded or parameterised path")
    parts = path.split("/")[1:]
    if not parts or any(p in ("", ".", "..") for p in parts):
        raise BadRequest("path has an empty or dot segment")
    version = None
    if _VERSION.match(parts[0]):
        version = parts[0]                 # whether it is a version this proxy serves is the policy's call, after authentication
        parts = parts[1:]
        if not parts:
            raise BadRequest("no endpoint")
    query: dict[str, str] = {}
    if qs:
        try:
            pairs = parse_qsl(qs, keep_blank_values=True, strict_parsing=True, max_num_fields=16, errors="strict")
        except ValueError:
            raise BadRequest("malformed query") from None
        for key, value in pairs:
            if key in query:
                raise BadRequest("repeated query key")
            if any(ord(c) < 0x20 or ord(c) == 0x7f for c in key + value) or len(value) > 4096:
                raise BadRequest("bad character in query")
            query[key] = value
    return "/" + "/".join(parts), tuple(parts), query, version


def content_length(head: Head, *, cap: int, allowed: bool) -> int:
    """The declared body length (0 if none). A body on a method that has none, or over the cap, is refused."""
    raw = head.headers.get("content-length")
    if raw is None:
        return 0
    if not _LENGTH.match(raw):
        raise BadRequest("bad Content-Length")
    length = int(raw)
    if length and not allowed:
        raise BadRequest("this request takes no body")
    if length > cap:
        raise Refusal("request body too large", 413)
    return length


def read_exact_body(conn: socket.socket, head: Head, length: int, *, timeout: float) -> bytes:
    """The whole body, exactly `length` bytes. Bytes beyond it are a pipelined second request: refused."""
    deadline = time.monotonic() + timeout
    body = head.leftover
    if len(body) > length:
        raise BadRequest("bytes after the request body")
    while len(body) < length:
        chunk = _deadline_recv(conn, min(65536, length - len(body)), deadline)
        if not chunk:
            raise BadRequest("body ended early")
        body += chunk
    return body


_CHUNK_SIZE = re.compile(rb"[0-9a-fA-F]{1,8}")


def iter_chunked(conn: socket.socket, leftover: bytes, *, cap: int, timeout: float):
    """The decoded pieces of a chunked request body, as they arrive. Strict: a hex size and nothing else on the size line (no
    extensions), exactly CRLF after each chunk's data, a last chunk of size 0 followed directly by the final CRLF (no
    trailers), and nothing after it (that would be a pipelined request). Over `cap` decoded bytes is refused."""
    deadline = time.monotonic() + timeout
    buf = bytearray(leftover)
    total = 0

    def fill() -> None:
        chunk = _deadline_recv(conn, 65536, deadline)
        if not chunk:
            raise BadRequest("body ended early")
        buf.extend(chunk)

    while True:
        while b"\r\n" not in buf:
            if len(buf) > 16:
                raise BadRequest("bad chunk framing")
            fill()
        line, _, _ = bytes(buf).partition(b"\r\n")
        if not _CHUNK_SIZE.fullmatch(line):
            raise BadRequest("bad chunk framing")
        size = int(line, 16)
        del buf[:len(line) + 2]
        if size == 0:
            while len(buf) < 2:
                fill()
            if bytes(buf) != b"\r\n":
                raise BadRequest("trailers or bytes after the last chunk")
            return
        total += size
        if total > cap:
            raise Refusal("request body too large", 413)
        remaining = size
        while remaining:
            if not buf:
                fill()
            take = min(len(buf), remaining)
            yield bytes(buf[:take])
            del buf[:take]
            remaining -= take
        while len(buf) < 2:
            fill()
        if bytes(buf[:2]) != b"\r\n":
            raise BadRequest("bad chunk framing")
        del buf[:2]


def strict_json(data: bytes, *, cap: int) -> dict:
    """A JSON object with no duplicate keys, no NaN/Infinity, nested at most MAX_JSON_DEPTH. Anything else is refused."""
    if len(data) > cap:
        raise Refusal("request body too large", 413)

    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def constant(_name):
        raise ValueError("non-finite number")

    try:
        doc = json.loads(data.decode("utf-8"), object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, RecursionError):
        raise BadRequest("malformed JSON body") from None
    if not isinstance(doc, dict) or _depth(doc) > MAX_JSON_DEPTH:
        raise BadRequest("the body must be a JSON object of bounded depth")
    return doc


def _depth(value, level: int = 1) -> int:
    if level > MAX_JSON_DEPTH + 1:
        return level
    if isinstance(value, dict):
        return max([_depth(v, level + 1) for v in value.values()] + [level])
    if isinstance(value, list):
        return max([_depth(v, level + 1) for v in value] + [level])
    return level


_REASONS = {200: "OK", 201: "Created", 204: "No Content", 304: "Not Modified", 400: "Bad Request", 401: "Unauthorized",
            403: "Forbidden", 404: "Not Found", 409: "Conflict", 413: "Payload Too Large", 500: "Internal Server Error",
            502: "Bad Gateway", 503: "Service Unavailable"}


def respond(conn: socket.socket, status: int, body: bytes = b"", *, content_type: str = "application/json",
            headers: dict[str, str] | None = None, length: int | None = None) -> None:
    """`length` overrides Content-Length (a HEAD answer describes a body it does not carry)."""
    head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}\r\nContent-Type: {content_type}\r\n"
            f"Content-Length: {len(body) if length is None else length}\r\n")
    for key, value in (headers or {}).items():
        head += f"{key}: {value}\r\n"
    head += "Connection: close\r\n\r\n"
    try:
        conn.settimeout(10)
        conn.sendall(head.encode("ascii") + body)
    except OSError:
        pass


def refuse(conn: socket.socket, status: int, reason: str) -> None:
    respond(conn, status, json.dumps({"message": f"planet-express docker proxy: {reason}"}).encode())


__all__ = [
    "MAX_HEAD",
    "BadRequest",
    "Head",
    "Refusal",
    "content_length",
    "normalise_target",
    "read_exact_body",
    "read_head",
    "refuse",
    "respond",
    "strict_json",
]
