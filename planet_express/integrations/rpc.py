"""Bounded, peer-authenticated Unix socket RPC for the dashboard."""

import grp
import json
import logging
import os
import re
import socket
import sqlite3
import stat
import struct
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from planet_express.application.command_service import CommandService, row_summary
from planet_express.core.store import INDEFINITE_EXPIRY, Store
from planet_express.execution import actions
from telegram_client import TelegramClient

MAX_FRAME = 1024 * 1024
# After a containers.routers read fails, answer at once for this long rather than tie up another
# RPC worker repeating it.
CONTAINER_ROUTERS_BACKOFF_SECONDS = 15
# How long a second containers.routers call waits for one in flight. Two dashboard tabs that
# refresh together should both get links; neither may hold a worker for a docker call.
CONTAINER_ROUTERS_WAIT_SECONDS = 0.5
log = logging.getLogger("planetexpress.rpc")


class RpcError(Exception):
    """RPC transport failure or a typed handler error."""

    def __init__(self, message: str, code: str = "internal"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _PostReply:
    result: Any
    callback: Callable[[], None]


def _error(request_id, code, message):
    return {"request_id": request_id, "ok": False, "error": {"code": code, "message": message}}


def _receive(conn, deadline):
    def exact(length):
        data = bytearray()
        while len(data) < length:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("frame read timed out")
            conn.settimeout(remaining)
            chunk = conn.recv(length - len(data))
            if not chunk:
                raise EOFError("connection closed without a complete frame")
            data.extend(chunk)
        return data

    length = struct.unpack("!I", exact(4))[0]
    if length > MAX_FRAME:
        raise EOFError("frame exceeds MAX_FRAME")
    return exact(length)


def _send(conn, response):
    data = json.dumps(response, allow_nan=False).encode("utf-8")
    if len(data) > MAX_FRAME:
        raise ValueError("response exceeds MAX_FRAME")
    conn.sendall(struct.pack("!I", len(data)) + data)


def _decode(data):
    def reject_constant(value):
        raise ValueError("invalid JSON constant")

    return json.loads(data.decode("utf-8"), parse_constant=reject_constant)


class RpcServer:
    def __init__(
        self, socket_path, handlers: dict[str, Callable[[dict], Any]],
        allowed_uids: set[int], group, read_timeout=5.0, workers=4,
        # Decisions only. Docker reads use general workers so slow reads cannot exhaust
        # the capacity that keeps approvals answerable (Codex review, T9).
        reserved_methods=frozenset({"approval.decide"}),
        reserved_read_timeout=0.5,
    ):
        if workers < 1 or read_timeout <= 0:
            raise ValueError("workers and read_timeout must be positive")
        self.socket_path = Path(socket_path)
        self.handlers = dict(handlers)
        self.allowed_uids = set(allowed_uids)
        self.group = group
        self.read_timeout = read_timeout
        self.reserved_methods = frozenset(reserved_methods)
        # The reserved slot is taken before the method is known, so an idle or trickling overflow
        # connection could hold it for the full read timeout and starve decisions. The only peer is
        # the dashboard, whose call() sends the whole frame at once, so a short deadline here
        # bounds that to well under a second without a separate reading pool (Codex review, T9).
        self.reserved_read_timeout = min(reserved_read_timeout, read_timeout)
        self._general = threading.BoundedSemaphore(workers)
        self._reserved = threading.BoundedSemaphore(1)
        self._pool = ThreadPoolExecutor(max_workers=workers + 1, thread_name_prefix="rpc")
        self._stopped = threading.Event()
        self._socket = None
        self._thread = None
        self._identity = None
        self._connections = set()
        self._lock = threading.Lock()

    def start(self):
        if self._socket is not None or self._stopped.is_set():
            raise RuntimeError("RPC server already started or stopped")
        if not self.socket_path.parent.is_dir():
            raise FileNotFoundError(f"socket directory does not exist: {self.socket_path.parent}")
        try:
            gid = grp.getgrnam(self.group).gr_gid if isinstance(self.group, str) else self.group
        except KeyError:
            raise LookupError(f"socket group does not exist: {self.group}") from None
        try:
            existing = self.socket_path.lstat()
        except FileNotFoundError:
            pass
        else:
            if not stat.S_ISSOCK(existing.st_mode):
                raise FileExistsError(f"socket path is not a socket: {self.socket_path}")
            # Only a refused connection proves the socket is stale. Unlinking a live one would
            # silently orphan a server that is still running (Codex review, T9).
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                probe.settimeout(0.5)
                try:
                    probe.connect(str(self.socket_path))
                except (ConnectionRefusedError, FileNotFoundError):
                    pass
                else:
                    raise FileExistsError(f"another RPC server is listening on {self.socket_path}")
            self.socket_path.unlink()
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Be explicit even though modern Python defaults to close-on-exec: config
            # activation re-execs core in place and the replacement must bind this path.
            conn.set_inheritable(False)
            old_umask = os.umask(0o007)
            try:
                conn.bind(str(self.socket_path))
            finally:
                os.umask(old_umask)
            info = self.socket_path.lstat()
            self._identity = (info.st_dev, info.st_ino)
            if gid is not None:
                os.chown(self.socket_path, -1, gid)
                os.chown(self.socket_path.parent, -1, gid)
            os.chmod(self.socket_path, 0o660)
            conn.listen(16)
            conn.settimeout(0.1)
            self._socket = conn
            self._thread = threading.Thread(target=self._accept, daemon=True, name="rpc-accept")
            self._thread.start()
        except Exception:
            conn.close()
            self._socket = None
            self._unlink()
            raise

    def _unlink(self):
        try:
            info = self.socket_path.lstat()
            if (info.st_dev, info.st_ino) == self._identity:
                self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _accept(self):
        while not self._stopped.is_set():
            try:
                conn, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            try:
                _, uid, _ = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                if uid not in self.allowed_uids:
                    log.warning("Rejected RPC peer uid %s", uid)
                    conn.close()
                    continue
                reserved = False
                slot = self._general
                if not slot.acquire(blocking=False):
                    slot = self._reserved
                    reserved = True
                    if not slot.acquire(blocking=False):
                        conn.settimeout(min(0.1, self.read_timeout))
                        _send(conn, _error(None, "busy", "RPC workers are busy"))
                        conn.close()
                        continue
                with self._lock:
                    self._connections.add(conn)
                try:
                    self._pool.submit(self._serve, conn, slot, reserved)
                except Exception:
                    with self._lock:
                        self._connections.discard(conn)
                    slot.release()
                    raise
            except Exception:  # noqa: BLE001 -- keep accepting after a connection failure
                conn.close()
                log.warning("RPC connection dispatch failed")

    def _serve(self, conn, slot, reserved):
        request_id = None
        post_reply = None
        try:
            read_timeout = self.reserved_read_timeout if reserved else self.read_timeout
            conn.settimeout(read_timeout)
            data = _receive(conn, time.monotonic() + read_timeout)
            try:
                request = _decode(data)
                if not isinstance(request, dict):
                    raise TypeError("request must be an object")
                candidate = request.get("request_id")
                if isinstance(candidate, str) and 1 <= len(candidate) <= 64:
                    request_id = candidate
                if (request_id is None or not isinstance(request.get("method"), str)
                        or not isinstance(request.get("params"), dict)):
                    raise ValueError("invalid request shape")
            except (TypeError, ValueError, UnicodeError, RecursionError):
                response = _error(request_id, "bad_request", "Invalid JSON request or request shape")
            else:
                method = request["method"]
                if reserved and method not in self.reserved_methods:
                    response = _error(request_id, "busy", "RPC workers are busy")
                elif method not in self.handlers:
                    response = _error(request_id, "unknown_method", "Unknown method")
                else:
                    try:
                        result = self.handlers[method](request["params"])
                        if isinstance(result, _PostReply):
                            post_reply = result.callback
                            result = result.result
                        response = {"request_id": request_id, "ok": True, "result": result}
                        # Serialization failures are handler failures too.
                        if len(json.dumps(response, allow_nan=False).encode("utf-8")) > MAX_FRAME:
                            raise ValueError("response exceeds MAX_FRAME")
                    except RpcError as exc:
                        response = _error(request_id, exc.code, str(exc))
                    except Exception as exc:  # noqa: BLE001 -- handler failures must not expose params
                        # Type name only: the message and traceback can carry request params.
                        log.warning("RPC handler failed for %s: %s", method, type(exc).__name__)
                        response = _error(request_id, "internal", "Internal error")
            conn.settimeout(self.read_timeout)
            _send(conn, response)
        except (OSError, EOFError):
            pass
        finally:
            if post_reply is not None:
                try:
                    post_reply()
                except Exception:  # noqa: BLE001 -- a post-reply failure cannot change the reply
                    log.warning("RPC post-reply hook failed")
            conn.close()
            with self._lock:
                self._connections.discard(conn)
            slot.release()

    def stop(self):
        self._stopped.set()
        if self._socket is not None:
            self._socket.close()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            for conn in self._connections:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        self._unlink()
        self._pool.shutdown(wait=True)


def call(socket_path, method, params=None, *, timeout=5.0, request_id=None) -> dict:
    """Make one framed request; transport failures raise RpcError."""
    request_id = uuid.uuid4().hex if request_id is None else request_id
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
            deadline = time.monotonic() + timeout
            conn.settimeout(timeout)
            conn.connect(str(socket_path))
            _send(conn, {"request_id": request_id, "method": method, "params": {} if params is None else params})
            response = _decode(_receive(conn, deadline))
            if not isinstance(response, dict):
                raise TypeError("response must be an object")
            return response
    except (OSError, EOFError, TypeError, ValueError, RecursionError) as exc:
        raise RpcError(str(exc)) from exc


def _params(params, strings, booleans=()):
    if not isinstance(params, dict) or set(params) != set(strings) | set(booleans):
        raise RpcError("Invalid params", "bad_request")
    for name, limit in strings.items():
        value = params[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise RpcError(f"Invalid {name}", "bad_request")
    for name in booleans:
        if not isinstance(params[name], bool):
            raise RpcError(f"Invalid {name}", "bad_request")


def build_core_handlers(
    commands: CommandService, store: Store, notifier=None, chat=None, config_service=None,
    *, start_scan=None,
) -> dict:
    def propose(params):
        _params(params, {"action": 128, "stack": 255, "service": 255, "requested_by": 256})
        return asdict(commands.propose(**params, requested_via="dashboard",
                                       timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS))

    def request_action(params):
        if not isinstance(params, dict):
            raise RpcError("Invalid params", "bad_request")
        action = params.get("action")
        if action == actions.RESTART_SERVICE:
            _params(params, {"action": 128, "stack": 255, "service": 255, "operator": 32})
        elif action in actions.STACK_ACTIONS:
            expected = {"action", "stack", "operator"}
            if set(params) == expected:
                _params(params, {"action": 128, "stack": 255, "operator": 32})
            elif set(params) == expected | {"service"} and params["service"] is None:
                _params({key: value for key, value in params.items() if key != "service"},
                        {"action": 128, "stack": 255, "operator": 32})
            else:
                raise RpcError("Invalid params", "bad_request")
        else:
            raise RpcError("Invalid action", "bad_request")
        auth_params({"operator": params["operator"]})
        if params["operator"] == "?":
            raise RpcError("Invalid operator", "bad_request")
        return asdict(commands.request_action(**params, timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS))

    def container(params):
        _params(params, {"stack": 255, "service": 255})
        # One budget for the whole call: resolution, facts and stats share the 4s, so the reply
        # beats the client's 5s deadline and a stats timeout still arrives typed (Codex review, T14).
        deadline = time.monotonic() + actions.RPC_DOCKER_TIMEOUT_SECONDS
        try:
            target = actions.resolve_target(**params, for_mutation=False,
                                            timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS)
        except actions.TargetTimeout:
            raise RpcError("host slow, retry", "timeout") from None
        except actions.TargetError as e:
            raise RpcError(str(e), "not_found") from None
        left = deadline - time.monotonic()
        facts = (actions.read_facts(target.container, timeout=left) if left > 0
                 else {"ok": False, "error": "timeout"})
        left = deadline - time.monotonic()
        vitals = (actions.read_stats(target.container, timeout=left) if left > 0
                  else {"ok": False, "error": "timeout"})
        return {"target": target.as_dict(),
                # The operator's pause list holds container NAMES and those containers are usually
                # `exited`, not docker-paused, so the state alone can't tell an intentional stop from
                # a failure. Say so here instead of letting the UI find out by attempting a restart
                # the policy then refuses (Codex review, T30).
                "paused": target.container in config.PAUSED_CONTAINERS,
                "vitals": vitals, "facts": facts,
                "actions": {actions.RESTART_SERVICE:
                            actions.REGISTRY[actions.RESTART_SERVICE].capabilities()}}

    def logs(params):
        if not isinstance(params, dict) or set(params) - {"stack", "service", "cursor", "cursor_hashes"}:
            raise RpcError("Invalid params", "bad_request")
        _params({k: v for k, v in params.items() if k not in {"cursor", "cursor_hashes"}},
                {"stack": 255, "service": 255})
        cursor = params.get("cursor")
        hashes = params.get("cursor_hashes", [])
        if "cursor" in params and (not isinstance(cursor, str) or not actions.LOG_TIMESTAMP_RE.fullmatch(cursor)):
            raise RpcError("Invalid cursor", "bad_request")
        if (not isinstance(hashes, list) or len(hashes) > actions.LOG_CURSOR_HASH_LIMIT
                or any(not isinstance(h, str) or re.fullmatch(r"[0-9a-f]{16}", h) is None for h in hashes)):
            raise RpcError("Invalid cursor_hashes", "bad_request")
        deadline = time.monotonic() + actions.RPC_DOCKER_TIMEOUT_SECONDS
        try:
            target = actions.resolve_target(params["stack"], params["service"], for_mutation=False,
                                            timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS)
        except actions.TargetTimeout:
            raise RpcError("host slow, retry", "timeout") from None
        except actions.TargetError:
            raise RpcError("Container not found", "not_found") from None
        left = deadline - time.monotonic()
        return (actions.read_logs(target.container, cursor=cursor, cursor_hashes=hashes, timeout=left)
                if left > 0 else {"ok": False, "error": "timeout"})

    def approval_shape(row):
        item = dict(row)
        item.pop("message_id", None)
        # row_summary(), not actions.action_summary(): the latter answers for legacy typed
        # actions only and raises on a planner runbook, which 503'd this whole endpoint.
        # Called before target_json is popped, because that is what it reads.
        item["summary"] = row_summary(item)
        item["target"] = json.loads(item.pop("target_json"))
        spec = actions.REGISTRY.get(item["action"])
        item["capabilities"] = spec.capabilities() if spec else {}
        if item.get("status") == "denied":
            actor = item.get("decided_by") or "unknown"
            item["denial_reason"] = f"Denied by {actor}."
            if actor == "policy":
                refusal = next((event for event in reversed(store.list_events(item["id"]))
                                if event["kind"] in {"approval.refused_by_policy",
                                                     "approval.refused_stale_incident"}), None)
                reason = refusal["payload"].get("reason") if refusal is not None else None
                if refusal is not None and refusal["kind"] == "approval.refused_stale_incident":
                    item["denial_reason"] = (
                        f"Incident evidence refused: {reason}"
                        if reason else "Incident evidence is no longer current."
                    )
                else:
                    item["denial_reason"] = (f"refused by current policy: {reason}"
                                             if reason else "Refused by current policy.")
        return item

    def approval_get(params):
        _params(params, {"approval_id": 64})
        deadline = time.monotonic() + actions.RPC_DOCKER_TIMEOUT_SECONDS
        try:
            row = store.get_approval(params["approval_id"], timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS)
        except sqlite3.OperationalError:
            raise RpcError("host slow, retry", "timeout") from None
        if row is None:
            raise RpcError("Approval not found", "not_found")
        item = approval_shape(row)
        left = deadline - time.monotonic()
        if left <= 0:
            raise RpcError("host slow, retry", "timeout")
        try:
            item["executions"] = store.list_executions(row["id"], timeout=left)
        except sqlite3.OperationalError:
            raise RpcError("host slow, retry", "timeout") from None
        return item

    def approval_recent(params):
        if (not isinstance(params, dict) or set(params) != {"limit"}
                or type(params["limit"]) is not int or not 1 <= params["limit"] <= 20):
            raise RpcError("Invalid limit", "bad_request")
        try:
            rows = store.list_recent_approvals(params["limit"], timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS)
        except sqlite3.OperationalError:
            raise RpcError("host slow, retry", "timeout") from None
        return [approval_shape(row) for row in rows]

    def pending(params):
        _params(params, {})
        result = []
        for row in store.list_pending():
            item = dict(row)
            item.pop("message_id", None)
            item["summary"] = row_summary(item)
            item["target"] = json.loads(item.pop("target_json"))
            spec = actions.REGISTRY.get(item["action"])
            item["capabilities"] = spec.capabilities() if spec else {}
            result.append(item)
        return result

    def decide(params):
        _params(params, {"approval_id": 64, "decided_by": 256}, ("approve",))
        return asdict(commands.decide(**params, decision=None))

    def status(params):
        _params(params, {"execution_id": 64})
        result = commands.get_status(params["execution_id"])
        if result is None:
            raise RpcError("Execution not found", "not_found")
        return result

    def _control_params(params):
        if not isinstance(params, dict) or set(params) != {"execution_id", "operator"}:
            raise RpcError("Invalid params", "bad_request")
        value = params["execution_id"]
        if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{12}", value) is None:
            raise RpcError("Invalid execution id", "bad_request")
        auth_params({"operator": params["operator"]})
        if params["operator"] == "?":
            raise RpcError("Invalid operator", "bad_request")

    def execution_abort(params):
        _control_params(params)
        return asdict(commands.abort(params["execution_id"], operator=params["operator"]))

    def execution_rollback(params):
        _control_params(params)
        return asdict(commands.rollback(params["execution_id"], operator=params["operator"]))

    def scan_start(params):
        """Start the same full pipeline Telegram's /check runs, and say so synchronously.

        The dashboard's SCAN control was an <a href="/"> that reloaded the page: it said SCAN
        and did not scan, and the Backups tab had a line of copy admitting it. This is the
        method behind the real one.
        """
        auth_params(params)
        if params["operator"] == "?":
            raise RpcError("Invalid operator", "bad_request")
        if start_scan is None:
            raise RpcError("Scanning is unavailable", "unavailable")
        return start_scan(params["operator"])

    def incident_params(params, *, item=False, operator=False):
        expected = {"incident_id"} if item else {"status", "limit"}
        if operator:
            expected.add("operator")
        if not isinstance(params, dict) or set(params) != expected:
            raise RpcError("Invalid params", "bad_request")
        if item:
            value = params["incident_id"]
            if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{12}", value) is None:
                raise RpcError("Invalid incident_id", "bad_request")
        else:
            if params["status"] not in {"open", "resolved", "all"}:
                raise RpcError("Invalid status", "bad_request")
            if type(params["limit"]) is not int or not 1 <= params["limit"] <= 100:
                raise RpcError("Invalid limit", "bad_request")
        if operator:
            auth_params({"operator": params["operator"]})
            if params["operator"] == "?":
                raise RpcError("Invalid operator", "bad_request")

    def incident_list(params):
        incident_params(params)
        try:
            return commands.list_incidents(**params, timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS)
        except actions.TargetTimeout:
            raise RpcError("host slow, retry", "timeout") from None

    def incident_get(params):
        incident_params(params, item=True)
        try:
            result = commands.get_incident_context(
                params["incident_id"], timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS
            )
        except actions.TargetTimeout:
            raise RpcError("host slow, retry", "timeout") from None
        if result is None:
            raise RpcError("Incident not found", "not_found")
        return result

    def incident_propose(params):
        incident_params(params, item=True, operator=True)
        result = commands.propose_incident(
            params["incident_id"], operator=params["operator"],
            deadline=time.monotonic() + actions.RPC_DOCKER_TIMEOUT_SECONDS,
        )
        if not result.ok and result.reason == "incident not found":
            raise RpcError("Incident not found", "not_found")
        return asdict(result)

    def auth_params(params, *, ip=False, step=False):
        expected = {"operator"} | ({"client_ip"} if ip else set()) | ({"step"} if step else set())
        if not isinstance(params, dict) or set(params) != expected:
            raise RpcError("Invalid params", "bad_request")
        operator = params["operator"]
        if not isinstance(operator, str) or re.fullmatch(r"[a-z0-9_.-]{1,32}|\?", operator) is None:
            raise RpcError("Invalid operator", "bad_request")
        if ip:
            value = params["client_ip"]
            if not isinstance(value, str) or not 1 <= len(value) <= 64 or not value.isprintable():
                raise RpcError("Invalid client_ip", "bad_request")
        if step and (type(params["step"]) is not int or not 0 <= params["step"] <= 2**63 - 1):
            raise RpcError("Invalid step", "bad_request")

    def notify_lock(operator, client_ip, locked_until):
        if notifier is None:
            return
        until = datetime.fromtimestamp(locked_until, timezone.utc).astimezone().strftime("%H:%M")
        try:
            notifier.notify(f"Dashboard login locked for {TelegramClient.s(operator)} "
                            f"from {TelegramClient.s(client_ip)} until {until}")
        except Exception:  # noqa: BLE001 -- never log exception URLs containing credentials
            log.warning("Failed to send dashboard lock notification")

    def auth_status(params):
        auth_params(params, ip=True)
        return store.auth_status(**params)

    def auth_failure(params):
        auth_params(params, ip=True)
        result = store.record_auth_failure(**params)
        if result["just_locked"] and notifier is not None:
            notify_lock(**params, locked_until=result["locked_until"])
        return result

    def auth_success(params):
        auth_params(params, ip=True)
        store.record_auth_success(**params)
        return {}

    def consume_step(params):
        auth_params(params, step=True)
        return {"accepted": store.consume_totp_step(**params)}

    def device_epoch(params):
        auth_params(params)
        return {"epoch": store.device_epoch(**params)}

    def notify_locked(params):
        auth_params(params, ip=True)
        status = store.auth_status(**params)
        if not status["locked"]:
            return {"sent": False, "reason": "not_locked"}
        if store.auth_lock_notified(**params, locked_until=status["locked_until"]):
            return {"sent": False, "reason": "already_notified"}
        notify_lock(**params, locked_until=status["locked_until"])
        return {"sent": True}

    def chat_operator(params):
        auth_params({"operator": params["operator"]})
        if params["operator"] == "?":
            raise RpcError("Invalid operator", "bad_request")

    def chat_ask(params):
        _params(params, {"operator": 32, "question": 2000, "submission_id": 64})
        chat_operator(params)
        if re.fullmatch(r"[A-Za-z0-9_-]{8,64}", params["submission_id"]) is None:
            raise RpcError("Invalid submission_id", "bad_request")
        try:
            return chat.ask(**params)
        except ValueError as exc:
            # ChatService validates too (e.g. a whitespace-only question); that is the caller's
            # mistake, not an internal error.
            raise RpcError(str(exc), "bad_request") from None

    def chat_get(params):
        _params(params, {"operator": 32, "ticket_id": 64})
        chat_operator(params)
        result = chat.get(**params)
        if result is None:
            raise RpcError("Ticket not found", "not_found")
        return result

    def chat_quota(params):
        _params(params, {})
        return chat.quota()

    def _iso(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, timezone.utc).astimezone().isoformat(timespec="seconds")

    def canary_candidates(params):
        """Open canary rollback windows, for the dashboard panel. The dashboard runs as
        `planetexpress-web`, which `scripts/web_access.py` denies access to the core's `data/`
        directory, so these rows can only reach it through here (Codex, T42)."""
        _params(params, {})
        return [
            {"stack": row["stack"], "service": row["service"],
             "old_image_id": row["old_image_id"], "image_reference": row["image_reference"],
             "recorded_at": _iso(row["created_at"]),
             "expires_at": "when a human closes it" if row["expires_at"] >= INDEFINITE_EXPIRY
                           else _iso(row["expires_at"])}
            for row in store.open_rollback_candidates(time.time())
        ]

    # ([[id, name, status], ...], answer -- or None for a final failure) from the last read, in
    # one slot so a reader never pairs one read's containers with another's answer.
    routers_cache = [None]
    # Monotonic time of the last failed read -- a hung or refusing docker daemon, or a read
    # that keeps failing -- so it costs one page load rather than every load an RPC worker.
    routers_backoff = [None]
    routers_lock = threading.Lock()
    # Consecutive reads that ended in a listing/inspect race. One is ordinary churn; two in a
    # row means the two views persistently disagree, which must back off like any failure.
    routers_races = [0]

    def container_routers(params):
        """{"routers": {router: container}, "services": {service: container}} for the
        dashboard's launch links (T46.1).

        Every call lists every container's id, name and status (one cheap `docker ps -a`) --
        the status string, not a running bool, which would let `restarting` and `exited`
        share a key. The labels behind the answer change only when a container is created
        or recreated (a new id), the names only by `docker rename`, and who declares anything
        only by a start or stop, so the inspect is reused exactly while that list is unchanged.

        One read at a time. A second call waits briefly for the first and then gives up with
        "timeout", so it can never park a bounded RPC worker for the length of a docker call.
        """
        _params(params, {})

        def backing_off():
            last = routers_backoff[0]
            return last is not None and time.monotonic() - last < CONTAINER_ROUTERS_BACKOFF_SECONDS

        if backing_off():
            raise RpcError("docker not answering, retry", "timeout")
        if not routers_lock.acquire(timeout=CONTAINER_ROUTERS_WAIT_SECONDS):
            raise RpcError("container read in progress", "timeout")
        try:
            return read_routers(backing_off)
        except RpcError:
            raise
        except Exception:
            # Anything unexpected from the docker helpers backs off like a failed read, or
            # every page load would repeat it.
            routers_backoff[0] = time.monotonic()
            raise
        finally:
            routers_lock.release()

    def read_routers(backing_off):
        """One read of container routers, under routers_lock (see container_routers)."""
        # Checked again: a call that waited out a failed read must not repeat it.
        if backing_off():
            raise RpcError("docker not answering, retry", "timeout")
        deadline = time.monotonic() + actions.RPC_DOCKER_TIMEOUT_SECONDS
        result = {"ok": False, "error": "unavailable"}
        # Twice at most: a `--rm` job exiting between the listing and the inspect fails the
        # inspect, and is ordinary enough that it should not blank the links for a load.
        for attempt in range(2):
            if attempt and deadline - time.monotonic() < 1:
                # Too little left to retry: a zero-budget call would report "timeout"
                # although the daemon answered the first time.
                break
            running = actions.list_containers(timeout=deadline - time.monotonic())
            if not running["ok"]:
                routers_backoff[0] = time.monotonic()
                raise RpcError("docker not answering, retry", "timeout"
                               if running["error"] == "timeout" else "unavailable")
            cached = routers_cache[0]
            if cached is not None and cached[0] == running["containers"]:
                routers_races[0] = 0
                if cached[1] is None:
                    # A final failure for exactly these containers: the same read would fail the
                    # same way, and log the same warning, until one of them changes.
                    raise RpcError("container routers unavailable", "unavailable")
                return cached[1]
            result = actions.read_router_owners([c[0] for c in running["containers"]],
                                                timeout=deadline - time.monotonic())
            if result["ok"] and result["containers"] != running["containers"]:
                # Renamed, started or stopped between the listing and the inspect: the answer
                # does not match the key it would be cached under. A race; read again.
                result = {"ok": False, "error": "unavailable", "race": True}
                continue
            if not result.get("race"):
                # Only churn is worth the second attempt; any other failure would repeat.
                break
        if not result["ok"]:
            # Any failed read backs off, not just a failed listing: a hung inspect or a
            # parse that keeps failing would otherwise cost every page load the full budget.
            if result.get("final"):
                routers_cache[0] = (running["containers"], None)
            # A race that simply ran out of retries is not a sick daemon -- once.
            routers_races[0] = routers_races[0] + 1 if result.get("race") else 0
            if not result.get("race") or routers_races[0] >= 2:
                routers_backoff[0] = time.monotonic()
            if result["error"] == "timeout":
                raise RpcError("host slow, retry", "timeout")
            raise RpcError("container routers unavailable", "unavailable")
        routers_backoff[0] = None
        routers_races[0] = 0
        answer = {"routers": result["routers"], "services": result["services"]}
        routers_cache[0] = (running["containers"], answer)
        return answer

    def widget_target(params):
        """Which widget a container has and where its API is, for the dashboard's fetcher
        (T46.3). Core resolves and hands over a widget NAME and addresses; the dashboard, which
        holds the keys and has no docker, fetches. Nothing here touches the widget's API.

        A planetexpress.widget label baked into the IMAGE may only disable a widget, never pick
        one: a keyed widget chosen by an image would send its key to whatever that image runs.
        """
        from planet_express.widgets import registry

        _params(params, {"stack": 255, "service": 255})
        deadline = time.monotonic() + actions.RPC_DOCKER_TIMEOUT_SECONDS
        try:
            target = actions.resolve_target(**params, for_mutation=False,
                                            timeout=actions.RPC_DOCKER_TIMEOUT_SECONDS)
        except actions.TargetTimeout:
            raise RpcError("host slow, retry", "timeout") from None
        except actions.TargetError as e:
            raise RpcError(str(e), "not_found") from None
        found = actions.read_widget_target(target.container, timeout=deadline - time.monotonic())
        if not found["ok"]:
            raise RpcError("host slow, retry" if found["error"] == "timeout"
                           else "container unavailable", found["error"])
        widgets = registry.load_widgets()
        label = (found["label"] or "").strip() or None
        if label is not None and found["label_from_image"] and label.lower() != registry.DISABLE:
            label = None
        matching = registry.widgets_matching(found["image"], widgets)
        if label is None and len(matching) > 1:
            # Two widgets claim this image (a second instance's widget): which key it should
            # get is not the image's to say. A label on the container picks.
            widget = None
        else:
            widget = registry.match_widget(found["image"], label=label, widgets=widgets)
        if widget is not None and widget.get("auth") is not None and (
                found.get("shares_namespace")
                or not any(w["name"] == widget["name"] for w in matching)
                or not registry.trusted_provenance(widget, found.get("pulled_from"))):
            # A KEY goes only to an image that is the widget's own -- by name AND by where it
            # was pulled from. Compose content (including files drafted for /install) sets both
            # the image name and labels; a registry digest from a trusted registry it cannot
            # fake. A local build has no digest and never gets a key. And never across a shared
            # network namespace: the key would go to the OWNER's address, an image nobody vetted.
            #
            # This is hardening, not a boundary. Compose also sets entrypoint, volumes and env:
            # an APPROVED compose can run the genuine image with its own listener on the port.
            # The trust boundary for widget keys is "containers the operator approved", the
            # same as for the docker socket such a compose could equally mount.
            widget = None
        return {"container": target.container, "container_id": found["id"],
                "running": found["running"], "widget": widget["name"] if widget else None,
                "addresses": found["addresses"] if widget else []}

    def config_enforced(params):
        """The editable values the dashboard shows, as this core process loaded them (T46.1).

        The dashboard read these from its own import. Config activation re-execs core only, and
        the dashboard's reload watcher follows the file, so what it showed could be either behind
        or ahead of what core enforces. Reading them from core shows what is enforced.
        """
        _params(params, {})
        return {"paused_containers": list(config.PAUSED_CONTAINERS),
                "backup_jobs": list(config.BACKUP_JOBS),
                "links": [dict(link) for link in config.LAUNCH_LINKS]}

    def config_get(params):
        _params(params, {})
        try:
            return config_service.get()
        except (OSError, ValueError):
            raise RpcError("Config unavailable") from None

    def config_validate(params):
        if not isinstance(params, dict) or set(params) != {"text"}:
            raise RpcError("Invalid params", "bad_request")
        text = params["text"]
        if not isinstance(text, str) or len(text.encode("utf-8", errors="surrogatepass")) > 256 * 1024:
            raise RpcError("Invalid text", "bad_request")
        result = config_service.validate(text)
        return {"ok": result.ok, "errors": result.errors,
                "changed_fields": result.changed_fields, "locked_fields": result.locked_fields}

    def config_apply(params):
        if not isinstance(params, dict) or set(params) != {"text", "base_sha256", "operator",
                                                           "elevated"}:
            raise RpcError("Invalid params", "bad_request")
        # Strictly a bool. Anything else is a caller that does not know what it is asserting,
        # and "truthy" is not a thing to authorise a sensitive config change on.
        if type(params["elevated"]) is not bool:
            raise RpcError("Invalid config apply params", "bad_request")
        text = params["text"]
        digest = params["base_sha256"]
        if (not isinstance(text, str)
                or len(text.encode("utf-8", errors="surrogatepass")) > 256 * 1024
                or not isinstance(digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise RpcError("Invalid config apply params", "bad_request")
        auth_params({"operator": params["operator"]})
        if params["operator"] == "?":
            raise RpcError("Invalid operator", "bad_request")
        result = config_service.apply(text, base_sha256=digest, operator=params["operator"],
                                      elevated=params["elevated"])
        callback = getattr(result, "_post_reply", None)
        public = result.as_dict()
        return _PostReply(public, callback) if callback is not None else public

    handlers = {"logs.tail": logs, "approval.get": approval_get,
            "approval.list_recent": approval_recent, "action.request": request_action, "query.container": container,
            "scan.start": scan_start,
            "proposal.create": propose, "proposal.list_pending": pending,
            "approval.decide": decide, "execution.get_status": status,
            "execution.abort": execution_abort, "execution.rollback": execution_rollback,
            "incident.list": incident_list, "incident.get": incident_get,
            "incident.propose": incident_propose,
            "auth.status": auth_status, "auth.record_failure": auth_failure,
            "auth.record_success": auth_success, "auth.consume_totp_step": consume_step,
            "auth.device_epoch": device_epoch, "auth.notify_locked": notify_locked,
            "canary.candidates": canary_candidates,
            "containers.routers": container_routers, "config.enforced": config_enforced,
            "query.widget_target": widget_target}

    if chat is not None:
        handlers.update({"chat.ask": chat_ask, "chat.get": chat_get, "chat.quota": chat_quota})
    if config_service is not None:
        handlers.update({"config.get": config_get, "config.validate": config_validate,
                         "config.apply": config_apply})
    return handlers
