"""A fake Docker daemon on a Unix socket, a fake inspector and a fake gate for the Docker proxy tests."""
import json
import os
import socket
import threading
from types import SimpleNamespace

import missions_helpers  # noqa: F401  (sets CASA_CONFIG for the config import)

from planet_express.missions.dockerproxy.httpwire import Refusal
from planet_express.missions.dockerproxy.policy import ContainerInfo, NetworkInfo, Scope

ID_A, ID_B, ID_C, ID_D, ID_E = ("a" * 64, "b" * 64, "c" * 64, "d" * 64, "e" * 64)
HOST = "host:local"


def labels(project, service=None):
    out = {"com.docker.compose.project": project}
    if service:
        out["com.docker.compose.service"] = service
    return out


CONTAINERS = {
    "qbittorrent": {"id": ID_A, "labels": labels("media", "qbittorrent"), "env": ["API_KEY=hunter2hunter2", "PUID=1000"]},
    "plex": {"id": ID_B, "labels": labels("media", "plex"), "env": []},
    "traefik": {"id": ID_C, "labels": labels("ingress", "traefik"), "env": []},
    "vault": {"id": ID_D, "labels": labels("media", "vault"), "env": []},       # protected by service id
    "stray": {"id": ID_E, "labels": {}, "env": []},
}
NETWORKS = {"media_default": labels("media"), "other_default": labels("other"), "bridge": {}, "host": {}}
VOLUMES = {"media_cfg": {"Driver": "local", "Options": None, "Labels": labels("media")},
           "bindvol": {"Driver": "local", "Options": {"type": "none", "o": "bind", "device": "/"}, "Labels": labels("media")},
           "other_vol": {"Driver": "local", "Options": None, "Labels": labels("other")},
           "fancy": {"Driver": "nfs", "Options": None, "Labels": labels("media")}}
IMAGES = {"lscr.io/linuxserver/qbittorrent:latest", "alpine:3"}


def make_scope(root, **over):
    paths = (f"{HOST}/path:{root}/stacks/media", f"{HOST}/mount:{root}/data")
    base = dict(resources=(f"{HOST}/stack:media", *paths), paths=paths, protected=(f"{HOST}/service:media/vault",),
                host="local", stacks_root=f"{root}/stacks", protected_gids=frozenset({0, 998}))
    base.update(over)
    return Scope(**base)


class FakeInspector:
    def __init__(self):
        self.calls = []

    def service_labels(self, project, service):
        self.calls.append(("service_labels", project, service))
        return [dict(c["labels"]) for c in CONTAINERS.values()
                if c["labels"].get("com.docker.compose.project") == project and c["labels"].get("com.docker.compose.service") == service]

    def container(self, ref):
        self.calls.append(("container", ref))
        for name, item in CONTAINERS.items():
            if ref in (name, item["id"], item["id"][:12]):
                return ContainerInfo(item["id"], name, item["labels"])
        return None

    def network(self, ref):
        self.calls.append(("network", ref))
        if ref in NETWORKS:
            return NetworkInfo("n" * 60 + ref[:4].ljust(4, "x"), ref, NETWORKS[ref])
        return None

    def volume(self, name):
        self.calls.append(("volume", name))
        return VOLUMES.get(name)

    def image_exists(self, ref):
        self.calls.append(("image", ref))
        return ref in IMAGES


class FakeGate:
    """Authenticates the token `good`; everything else is a Gate the tests can switch."""

    def __init__(self, session_id="S-1"):
        self.session = SimpleNamespace(id=session_id, fences={})
        self.paused = False
        self.write_refusal: str | None = None
        self.alive_flag = True
        self.events: list[dict] = []
        self.fail_record = False
        self.peers = []

    def authenticate(self, peer, token):
        self.peers.append(peer)
        return self.session if token == "good" else None

    def check_live(self, session):
        if self.paused:
            raise Refusal("the session is paused", 503)

    def check_write(self, session):
        if self.paused:
            raise Refusal("the session is paused", 503)
        if self.write_refusal:
            raise Refusal(self.write_refusal)

    def alive(self, session):
        return self.alive_flag and not self.paused

    def record(self, session, body):
        if self.fail_record:
            raise RuntimeError("disk full")
        self.events.append(body)

    def writes(self):
        return [e for e in self.events if e.get("event") == "docker.write"]


class FakeDaemon:
    """Answers the handful of endpoints the proxy forwards to; records every request it receives."""

    def __init__(self, path, stream_bytes=0):
        self.path, self.requests, self.stream_bytes = path, [], stream_bytes
        self.exec_ids = []
        self.api_version = "1.43"               # what this daemon says in its Api-Version header
        self.made = {}                          # containers created through this daemon: id -> {name, labels, host_config}
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(2)
        self._sock.close()

    def writes(self):
        return [r for r in self.requests if r["method"] in ("POST", "PUT", "DELETE")]

    def _loop(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _read_head(conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None, b""
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        return head.decode("latin-1"), rest

    def _serve(self, conn):
        try:
            head, rest = self._read_head(conn)
            if head is None:
                return
            lines = head.split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
            length = int(headers.get("content-length", "0") or 0)
            body = rest
            if headers.get("transfer-encoding", "").lower() == "chunked":
                body = self._dechunk(conn, rest)
            while len(body) < length:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                body += chunk
            record = {"method": method, "target": target, "headers": headers, "body": body}
            self.requests.append(record)
            self._route(conn, record)
        except OSError:
            pass
        finally:
            conn.close()

    @staticmethod
    def _dechunk(conn, rest):
        buf, out = rest, b""
        while True:
            while b"\r\n" not in buf:
                buf += conn.recv(65536)
            line, _, buf = buf.partition(b"\r\n")
            size = int(line, 16)
            if size == 0:
                return out
            while len(buf) < size + 2:
                buf += conn.recv(65536)
            out, buf = out + buf[:size], buf[size + 2:]

    def _send(self, conn, status, doc=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else (json.dumps(doc).encode() if doc is not None else b"")
        reason = {200: "OK", 201: "Created", 204: "No Content", 404: "Not Found"}.get(status, "X")
        conn.sendall((f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
                      f"Api-Version: {self.api_version}\r\nConnection: close\r\n\r\n").encode() + body)

    def _route(self, conn, rec):
        method, target = rec["method"], rec["target"]
        path = target.split("?")[0]
        parts = path.split("/")
        if parts[1].startswith("v1."):
            parts = [""] + parts[2:]
        tail = "/".join(parts[1:])
        if tail == "_ping":
            return self._send(conn, 200, raw=b"OK", ctype="text/plain")
        if tail == "containers/json":
            return self._send(conn, 200, [
                {"Id": c["id"], "Names": ["/" + n], "Labels": c["labels"], "Image": "x"} for n, c in CONTAINERS.items()])
        if tail == "containers/create":
            body = json.loads(rec["body"] or b"{}")
            cid = os.urandom(32).hex()
            self.made[cid] = {"name": "made-" + cid[:6], "labels": body.get("Labels", {}), "host_config": body.get("HostConfig", {})}
            return self._send(conn, 201, {"Id": cid, "Warnings": []})
        if parts[1] == "containers" and len(parts) >= 3:
            ref = parts[2]
            found = next((n for n, c in CONTAINERS.items() if ref in (n, c["id"], c["id"][:12])), None)
            made = self.made.get(ref)
            if made is not None:
                found = made["name"]
                CONTAINERS_VIEW = {found: {"id": ref, "labels": made["labels"], "env": [], "host_config": made["host_config"]}}
            else:
                CONTAINERS_VIEW = CONTAINERS
            op = parts[3] if len(parts) > 3 else ""
            if found is None and op != "":
                return self._send(conn, 404, {"message": "no such container"})
            if found is None and method == "GET" and op == "":
                return self._send(conn, 404, {"message": "no such container"})
            c = CONTAINERS_VIEW.get(found)
            if op == "json":
                if c is None:
                    return self._send(conn, 404, {"message": "no such container"})
                return self._send(conn, 200, {"Id": c["id"], "Name": "/" + found, "Config": {"Labels": c["labels"], "Env": c["env"],
                                                                                              "Cmd": ["run", "--password=hunter2hunter2"]},
                                              "HostConfig": c.get("host_config", {}), "Mounts": c.get("mounts", [])})
            if op == "logs" or op == "stats":
                payload = b"x" * self.stream_bytes
                conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/vnd.docker.raw-stream\r\n\r\n" + payload)
                if self.stream_bytes == 0:
                    threading.Event().wait(0.5)
                return None
            if op == "exec":
                eid = os.urandom(32).hex()
                self.exec_ids.append(eid)
                return self._send(conn, 201, {"Id": eid})
            if op == "attach":
                return self._hijack(conn, rec)
            if op == "archive":
                body = b"TARDATA"
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/x-tar\r\nX-Docker-Container-Path-Stat: eyJuYW1lIjoiY29uZmlnIn0=\r\n"
                             + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                return None
            return self._send(conn, 204)
        if parts[1] == "exec" and len(parts) >= 4 and parts[3] == "start":
            return self._hijack(conn, rec)
        if parts[1] == "exec" and len(parts) >= 4 and parts[3] == "json":
            return self._send(conn, 200, {"ID": parts[2], "Running": False})
        if parts[1] == "networks" and len(parts) == 3:
            if parts[2] not in NETWORKS:
                return self._send(conn, 404, {"message": "no such network"})
            return self._send(conn, 200, {"Id": "n" * 60 + parts[2][:4].ljust(4, "x"), "Name": parts[2], "Labels": NETWORKS[parts[2]]})
        if parts[1] == "networks" and len(parts) == 4:
            return self._send(conn, 204)
        if tail == "networks":
            return self._send(conn, 200, [{"Name": n, "Id": "i" * 60 + n[:4].ljust(4, "x"), "Labels": lab, "Options": {"x": "y"}} for n, lab in NETWORKS.items()])
        if parts[1] == "volumes" and len(parts) == 3:
            if parts[2] not in VOLUMES:
                return self._send(conn, 404, {"message": "no such volume"})
            return self._send(conn, 200, {"Name": parts[2], "Mountpoint": f"/var/lib/docker/volumes/{parts[2]}/_data", **VOLUMES[parts[2]]})
        if tail == "volumes":
            return self._send(conn, 200, {"Volumes": [{"Name": n, "Mountpoint": f"/var/lib/docker/volumes/{n}/_data", **v} for n, v in VOLUMES.items()]})
        if parts[1] == "images" and len(parts) >= 4 and parts[-1] == "json":
            ref = "/".join(parts[2:-1])
            if ref not in IMAGES:
                return self._send(conn, 404, {"message": "no such image"})
            return self._send(conn, 200, {"Id": "sha256:" + "1" * 64, "Config": {"Env": ["TOKEN=abcdef123456"]}})
        if tail == "images/json":
            return self._send(conn, 200, [{"Id": "sha256:" + "1" * 64, "RepoTags": ["alpine:3"]}])
        if tail == "events":
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n" + b'{"Type":"container"}\n' * 3)
            return None
        return self._send(conn, 404, {"message": "unknown to the fake"})

    def _hijack(self, conn, rec):
        upgrading = rec["headers"].get("upgrade", "").lower() == "tcp"
        if upgrading:
            conn.sendall(b"HTTP/1.1 101 UPGRADED\r\nContent-Type: application/vnd.docker.raw-stream\r\n"
                         b"Connection: Upgrade\r\nUpgrade: tcp\r\n\r\n")
        else:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/vnd.docker.raw-stream\r\n\r\n")
        conn.sendall(b"HELLO")
        conn.settimeout(2)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(b"echo:" + data)
        except OSError:
            return


def talk(path, payload: bytes, *, read_until_close=True, timeout=5.0):
    """Send raw bytes to a Unix socket and return (status, headers, body) of the one response."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(path)
    try:
        sock.sendall(payload)
    except OSError:                        # the proxy refused early (a body over its cap) and closed: read what it said
        pass
    data = b""
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk
    except OSError:
        pass
    finally:
        sock.close()
    return parse(data)


def parse(data: bytes):
    if not data:
        return 0, {}, b""
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split(" ")[1])
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return status, headers, body


def request(path, method, target, *, body=None, headers=None, token="good", version="v1.43", raw_target=False):
    """Build and send one well-formed request. `target` has no version unless raw_target."""
    head = {"Host": "docker", "X-PE-Session": token} if token else {"Host": "docker"}
    head.update(headers or {})
    payload = b""
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        head.setdefault("Content-Type", "application/json")
        head["Content-Length"] = str(len(payload))
    full = target if raw_target else (f"/{version}{target}" if version else target)
    text = f"{method} {full} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in head.items()) + "\r\n"
    return talk(path, text.encode() + payload)


def create_body(**over):
    body = {"Image": "lscr.io/linuxserver/qbittorrent:latest", "Cmd": ["run"], "Env": ["PUID=1000"],
            "Labels": labels("media", "qbittorrent"), "HostConfig": {"RestartPolicy": {"Name": "unless-stopped"}}}
    body.update(over)
    return body
