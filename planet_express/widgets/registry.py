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
        try:
            widget = _validated(module.name, getattr(spec, "WIDGET", None),
                                getattr(spec, "summarise", None))
        except Exception:
            # The validator runs in the request path too, so it gets the same treatment as
            # the import: a declaration malformed in a way it did not anticipate costs its
            # own panel. The guarantee cannot rest on the checker being exhaustive.
            log.warning("Skipping widget module %s: its contract could not be validated",
                        module.name, exc_info=True)
            continue
        if widget is not None:
            found[widget["name"]] = widget
    return found


# The shapes the fetcher knows how to turn into a request, and the fields each one needs.
# A widget may declare no auth at all -- plenty of APIs need none -- but a declaration the
# fetcher cannot act on is a broken widget, not an unauthenticated one.
_AUTH_FIELDS = {"header": ("header", "env"), "basic": ("username_env", "password_env")}


def _auth_problem(auth) -> str | None:
    if auth is None:
        return None
    if not isinstance(auth, dict):
        return "auth must be a dict"
    kind = auth.get("type")
    # isinstance before the lookup: `{"type": []}` is unhashable and .get() raises TypeError
    # on it rather than returning None.
    required = _AUTH_FIELDS.get(kind) if isinstance(kind, str) else None
    if required is None:
        return f"unknown auth type {kind!r}"
    missing = [field for field in required
               if not isinstance(auth.get(field), str) or not auth[field].strip()]
    return f"auth {auth['type']} is missing {', '.join(missing)}" if missing else None


def _validated(module_name: str, widget, summarise) -> dict | None:
    """A widget with a malformed contract is skipped here, not discovered later.

    Importing cleanly is not the same as being usable: `match: None` iterates fine at import
    and then raises TypeError inside match_widget(), which runs once per container in the
    dashboard's request path -- so one bad declaration would take the whole page down, which
    is the exact opposite of the per-widget isolation this module promises. Every field the
    matcher and the fetcher will read is checked once, here, where the cost is one skipped
    panel.
    """
    if not isinstance(widget, dict):
        return None
    name = widget.get("name")
    match = widget.get("match")
    paths = widget.get("get")
    problem = None
    if not isinstance(name, str) or not name.strip():
        problem = "no usable name"
    elif not isinstance(match, (list, tuple)) or not match or not all(
            isinstance(entry, str) and entry.strip() for entry in match):
        problem = "match must be a non-empty list of image strings"
    elif not isinstance(widget.get("port"), int) or isinstance(widget.get("port"), bool):
        problem = "port must be an int"
    elif not isinstance(paths, (list, tuple)) or not paths or not all(
            isinstance(path, str) and path.startswith("/")
            and "://" not in path and ".." not in path for path in paths):
        problem = "get must be a non-empty list of rooted paths"
    elif not callable(summarise):
        problem = "no summarise()"
    else:
        problem = _auth_problem(widget.get("auth"))
    if problem is not None:
        log.warning("Skipping widget module %s: %s", module_name, problem)
        return None
    return {**widget, "name": name, "match": list(match), "get": list(paths),
            "summarise": summarise}


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
