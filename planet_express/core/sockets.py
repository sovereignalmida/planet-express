"""Ending a blocked socket read from another thread.

Two callers need this: the widget fetcher, talking to a container's API, and the icon cache,
talking to a CDN. Both drive http.client, and neither can bound a call with timeouts alone --
a socket timeout is an INACTIVITY timeout, so a peer trickling one byte inside it holds the
read forever, and getresponse() parses headers before any deadline check can run.

close() is not enough. http.client hands the socket to the response the moment it reads one
that will close the connection, drops its own reference, and the read goes on regardless.
shutdown() acts on the connection itself, so whichever object now holds the descriptor, the
blocked recv returns at once.
"""

import socket


def shutdown_sock(sock) -> None:
    """End any read blocked on this socket. Safe to call from another thread, and twice."""
    if sock is not None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
