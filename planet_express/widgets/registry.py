"""Which widget, if any, belongs to a container — and nothing else.

Deliberately free of I/O. Deciding *which* widget a container gets is a pure question about
its image and its labels; fetching anything from that widget's API is a separate concern with
a security surface, and lives in its own module so this one can be held to unit tests.

A widget never changes container health. It is a readout of what an app is doing, shown once
the container is already known to be healthy, and an unreachable API dims the widget and
nothing else.
"""

import importlib
import logging
import pkgutil

log = logging.getLogger("planetexpress.widgets")

# Registry prefixes that say where an image came from, not what it is. The same application
# appears on this host under three of them -- lscr.io/linuxserver/sonarr, linuxserver/radarr
# and ghcr.io/linuxserver/qbittorrent are all the linuxserver images -- so a match list of
# literal strings would miss two of the three.
_REGISTRY_MARKERS = (".", ":")

DISABLE = "none"


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


def load_widgets() -> dict:
    """Every widgets/<name>.py that declares a WIDGET dict, keyed by its name.

    A widget is a file. Adding one is adding a file, which is the whole point of the contract:
    no template change, no registry edit, nothing to forget to update in two places.
    """
    from planet_express import widgets as package

    found = {}
    for module in pkgutil.iter_modules(package.__path__):
        if module.name.startswith("_") or module.name == "registry":
            continue
        try:
            spec = importlib.import_module(f"{package.__name__}.{module.name}")
        except Exception:
            # One broken widget file must cost its own panel and nothing else. This is called
            # from the dashboard's request path, where an import error would otherwise be a
            # 500 on a page whose whole contract is that it always renders.
            log.warning("Skipping widget module %s: it failed to import", module.name,
                        exc_info=True)
            continue
        widget = getattr(spec, "WIDGET", None)
        if not isinstance(widget, dict) or not widget.get("name"):
            continue
        found[widget["name"]] = {**widget, "summarise": getattr(spec, "summarise", None)}
    return found


def match_widget(image: str, *, label: str | None = None, widgets: dict | None = None):
    """The widget for one container, or None.

    `label` is the container's `planetexpress.widget` value and wins over the image match, in
    both directions: it names a widget the image would not have matched, and `none` refuses a
    widget the image would have matched.

    Matching is on the whole normalised repo, never a substring. This host runs both
    `ghcr.io/immich-app/immich-server` and `ghcr.io/varun-raj/immich-power-tools`; a substring
    match on "immich" would hang the immich widget on a tool that has no such API.
    """
    available = load_widgets() if widgets is None else widgets
    if label:
        cleaned = label.strip().lower()
        if cleaned == DISABLE:
            return None
        return available.get(cleaned)

    repo = normalise_image(image)
    if not repo:
        return None
    for widget in available.values():
        if repo in {normalise_image(candidate) for candidate in widget.get("match", ())}:
            return widget
    return None
