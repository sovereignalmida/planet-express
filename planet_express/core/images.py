"""Reading a docker image reference for what application it is.

One rule, in one place, because two callers already need it: the widget registry decides
which widget a container gets, and casa_leela finds gluetun's container by image rather than
by name (this host's is spelled CASA_GLUETON, a typo that has outlived several rebuilds).
"""

# Registry prefixes say where an image came from, not what it is. The same application
# appears on this host under three of them -- lscr.io/linuxserver/sonarr, linuxserver/radarr
# and ghcr.io/linuxserver/qbittorrent are all the linuxserver images -- so a match list of
# literal strings would miss two of the three.
_REGISTRY_MARKERS = (".", ":")


def normalise_image(image: str) -> str:
    """'lscr.io/linuxserver/sonarr:latest' -> 'linuxserver/sonarr'.

    Strips a digest, a tag and a registry host. Keeps the rest verbatim, because the rest is
    the only part that identifies the application.
    """
    if not isinstance(image, str) or not image.strip():
        return ""
    repo = image.strip().split("@", 1)[0]
    head, separator, tail = repo.rpartition(":")
    # A tag, not a registry port: a port is followed by a path, a tag never is.
    if separator and "/" not in tail:
        repo = head
    parts = [part for part in repo.split("/") if part]
    if len(parts) > 1 and (any(marker in parts[0] for marker in _REGISTRY_MARKERS)
                           or parts[0] == "localhost"):
        parts = parts[1:]
    return "/".join(parts)
