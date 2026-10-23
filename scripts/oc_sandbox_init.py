#!/usr/bin/python3
"""PID 1 inside the OpenCode delegation sandbox (stdlib only; started by scripts/oc_delegate.py through bubblewrap).

The sandbox has no network: its only route out is a Unix socket, bind-mounted from the host, that a credential-injecting
proxy listens on. This process makes that socket reachable as a loopback TCP port (OpenCode's provider needs a URL),
then runs the worker command and returns its exit status. It holds no credentials and does nothing else.

    oc_sandbox_init.py --port N --socket /run/oc/proxy.sock -- COMMAND [ARG...]
"""
import argparse
import os
import signal
import socket
import socketserver
import subprocess
import sys
import threading


def pump(src: socket.socket, dst: socket.socket) -> None:
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


class Bridge(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port: int, path: str):
        self.path = path
        super().__init__(("127.0.0.1", port), BridgeHandler)


class BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            upstream.connect(self.server.path)
        except OSError:
            upstream.close()
            return
        a = threading.Thread(target=pump, args=(self.request, upstream), daemon=True)
        b = threading.Thread(target=pump, args=(upstream, self.request), daemon=True)
        a.start()
        b.start()
        a.join()
        b.join()
        upstream.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print("no command", file=sys.stderr)
        return 2
    bridge = Bridge(args.port, args.socket)
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    child = subprocess.Popen(command, stdin=subprocess.DEVNULL, start_new_session=True)

    def forward(signum, _frame):
        try:
            os.killpg(child.pid, signum)
        except OSError:
            pass
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, forward)
    code = child.wait()
    return 128 + (-code) if code < 0 else code


if __name__ == "__main__":
    sys.exit(main())
