"""Which widget, if any, belongs to a container — and nothing else.

Deliberately free of I/O. Deciding *which* widget a container gets is a pure question about
its image and its labels; fetching anything from that widget's API is a separate concern with
a security surface, and lives in its own module so this one can be held to unit tests.

A widget never changes container health. It is a readout of what an app is doing, shown once
the container is already known to be healthy, and an unreachable API dims the widget and
nothing else.
"""

import hashlib
import logging
import os
import re
import sys
import threading

log = logging.getLogger("planetexpress.widgets")

# Registry prefixes that say where an image came from, not what it is. The same application
# appears on this host under three of them -- lscr.io/linuxserver/sonarr, linuxserver/radarr
# and ghcr.io/linuxserver/qbittorrent are all the linuxserver images -- so a match list of
# literal strings would miss two of the three.
_REGISTRY_MARKERS = (".", ":")
# What a widget's GET path may contain: a rooted path and a plain query string. No scheme, no
# authority, no fragment, no whitespace -- the fetcher appends it to http://<address>:<port>
# and nothing in it may change where that goes.
_PATH_RE = re.compile(r"/[A-Za-z0-9._~/\-]*(\?[A-Za-z0-9._~=&%\-]*)?")

DISABLE = "none"
_DOCKER_HUB_ALIASES = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
# The most a widget may declare it needs for a whole fetch. Parsed JSON costs ~25x its wire
# size, so this is a memory ceiling as much as a bandwidth one: 1 MiB is ~25 MB parsed. No
# shipped widget declares more than the fetcher's 256 KiB default; one wanting more than
# this should find a lighter endpoint.
MAX_RESPONSE_BYTES_LIMIT = 1024 * 1024


def split_image(image: str) -> tuple[str, str]:
    """'lscr.io/linuxserver/sonarr:latest' -> ('lscr.io', 'linuxserver/sonarr').

    Strips a digest and a tag; a reference with no registry host is Docker Hub's. The one
    place a registry is told apart from a repo path, so matching and provenance agree.
    """
    if not isinstance(image, str) or not image.strip():
        return "", ""
    repo = image.strip().split("@", 1)[0]
    head, separator, tail = repo.rpartition(":")
    # A tag, not a registry port: a port is followed by a path, a tag never is.
    if separator and "/" not in tail:
        repo = head
    parts = [part for part in repo.split("/") if part]
    host = "docker.io"
    if len(parts) > 1 and (any(marker in parts[0] for marker in _REGISTRY_MARKERS)
                           or parts[0] == "localhost"):
        host, parts = parts[0].lower(), parts[1:]
    if host in _DOCKER_HUB_ALIASES:
        host = "docker.io"
        if len(parts) == 2 and parts[0] == "library":
            parts = parts[1:]
    return host, "/".join(parts)


def normalise_image(image: str) -> str:
    """'lscr.io/linuxserver/sonarr:latest' -> 'linuxserver/sonarr'.

    Strips a digest, a tag and a registry host. Keeps the rest verbatim, because the rest is
    the only part that identifies the application.
    """
    return split_image(image)[1]


_loaded: dict = {}
_loaded_lock = threading.Lock()


_NOT_WIDGETS = ("registry", "fetcher")


def _read_sources(directory: str) -> dict:
    """{module name: source bytes} for every widget file -- read ONCE, so the cache key and
    the code that runs are the same bytes. A file that cannot be read (a dangling editor lock
    link, a root-only file) is skipped on its own rather than disabling the cache."""
    sources = {}
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return sources
    for entry in entries:
        name = entry.name[:-3]
        if (not entry.name.endswith(".py") or name.startswith("_") or name in _NOT_WIDGETS):
            continue
        try:
            if not entry.is_file(follow_symlinks=False):
                continue
            with open(entry.path, "rb") as handle:
                sources[name] = handle.read()
        except OSError:
            continue
    return sources


def load_widgets() -> dict:
    """Every widgets/<name>.py that declares a WIDGET dict, keyed by its name.

    A widget is a file. Adding one is adding a file, which is the whole point of the contract:
    no template change, no registry edit, nothing to forget to update in two places.

    Loaded once per set of files: this runs on the dashboard's request path and core's, and
    re-importing and re-validating every file each time -- a broken one logging a traceback
    each time -- is cost for nothing. Any file added, removed or changed loads afresh.
    """
    from planet_express import widgets as package

    # All under the lock: importlib.reload() hands a second caller a module another thread is
    # still re-running, so two concurrent loads could cache the pre-edit declaration under the
    # post-edit key -- and keep sending a key by it until the next edit.
    with _loaded_lock:
        sources = _read_sources(package.__path__[0])
        # Content, not mtime: a same-size edit inside one timestamp tick would otherwise keep
        # the old declaration -- and route keys by it.
        key = tuple(sorted((name, hashlib.sha256(source).hexdigest())
                           for name, source in sources.items()))
        if "key" in _loaded and _loaded["key"] == key:
            return dict(_loaded["widgets"])
        found = _load_widgets(package, sources, previous=_loaded.get("names", ()))
        _loaded.update(key=key, widgets=found, names=tuple(sources))
        return dict(found)


def _forget(package, name: str) -> None:
    sys.modules.pop(f"{package.__name__}.{name}", None)
    if isinstance(getattr(package, name, None), type(sys)):
        delattr(package, name)


def _exec_widget_file(package, name: str, source: bytes):
    """A widget file run into a FRESH namespace, from its source. Not importlib.reload(): that
    keeps names the edited file no longer defines (delete WIDGET and the old one stays) and
    trusts a .pyc stamped to the second."""
    import types

    qualified = f"{package.__name__}.{name}"
    path = os.path.join(package.__path__[0], f"{name}.py")
    module = types.ModuleType(qualified)
    module.__file__ = path
    module.__package__ = package.__name__
    # Registered, so dataclasses, typing and relative imports see an ordinary module -- but a
    # new object each load, so nothing from the previous version of the file survives.
    sys.modules[qualified] = module
    try:
        exec(compile(source, path, "exec"), module.__dict__)  # noqa: S102 -- our own widget files
    except BaseException:
        # Both import forms must agree the old version is gone.
        _forget(package, name)
        raise
    # And the package attribute, so `from planet_express.widgets import sonarr` is this module.
    setattr(package, name, module)
    return module


def _load_widgets(package, sources: dict, previous=()) -> dict:
    found = {}
    duplicated = set()
    for gone in set(previous) - set(sources):
        _forget(package, gone)
    for name in sorted(sources):
        try:
            spec = _exec_widget_file(package, name, sources[name])
        except Exception:
            # One broken widget file must cost its own panel and nothing else. This is called
            # from the dashboard's request path, where an import error would otherwise be a
            # 500 on a page whose whole contract is that it always renders.
            log.warning("Skipping widget module %s: it failed to import", name,
                        exc_info=True)
            continue
        try:
            widget = _validated(name, getattr(spec, "WIDGET", None),
                                getattr(spec, "summarise", None))
        except Exception:
            # The validator runs in the request path too, so it gets the same treatment as
            # the import: a declaration malformed in a way it did not anticipate costs its
            # own panel. The guarantee cannot rest on the checker being exhaustive.
            log.warning("Skipping widget module %s: its contract could not be validated",
                        name, exc_info=True)
            continue
        if widget is not None:
            if widget["name"] in found or widget["name"] in duplicated:
                # Two files, one name: the later would silently replace the earlier, key and
                # all. Neither loads.
                log.warning("Skipping widget %s: declared by more than one file", widget["name"])
                found.pop(widget["name"], None)
                duplicated.add(widget["name"])
                continue
            found[widget["name"]] = widget
    # Two names that give one env prefix (`foo-bar`, `foo_bar`) would read the same keys and
    # send them to two different applications. Neither is loaded.
    prefixes: dict = {}
    for name in found:
        prefixes.setdefault(name.upper().replace("-", "_"), []).append(name)
    for names in prefixes.values():
        if len(names) > 1:
            log.warning("Skipping widgets %s: their key names would collide", ", ".join(names))
            for name in names:
                found.pop(name, None)
    return found


# The shapes the fetcher knows how to turn into a request, and the fields each one needs.
# A widget may declare no auth at all -- plenty of APIs need none -- but a declaration the
# fetcher cannot act on is a broken widget, not an unauthenticated one.
_AUTH_FIELDS = {"header": ("header", "env"), "basic": ("username_env", "password_env")}


# Names a widget may never read, whatever it is called: the dashboard's own login secrets, and
# core's -- casa-dashboard.service keeps the latter out of this process, and a widget must not
# be the way one gets sent to a container if that ever slips.
_RESERVED_ENV_PREFIXES = ("PE_", "CASA_", "TG_", "TELEGRAM_", "ANTHROPIC_", "OPENAI_", "LLM_")


_ENV_SUFFIXES = ("API_KEY", "TOKEN", "USERNAME", "PASSWORD")


def env_name_problem(widget_name: str, env: str) -> str | None:
    """A widget reads only `<ITS NAME>_<API_KEY|TOKEN|USERNAME|PASSWORD>`: sonarr reads
    SONARR_API_KEY, adguard ADGUARD_USERNAME. Whatever it reads is sent to a container, so the
    name is the whole allowlist -- exact, so `sonarr` can never read SONARR_4K_API_KEY, which
    belongs to a second instance and would be sent to the first."""
    prefix = widget_name.upper().replace("-", "_") + "_"
    if env not in {prefix + suffix for suffix in _ENV_SUFFIXES}:
        return f"env name {env!r} must be {prefix}<{'|'.join(_ENV_SUFFIXES)}>"
    if env.startswith(_RESERVED_ENV_PREFIXES):
        return f"env name {env!r} is reserved"
    return None


def auth_env_names(auth) -> list[str]:
    if not isinstance(auth, dict):
        return []
    fields = _AUTH_FIELDS.get(auth.get("type")) if isinstance(auth.get("type"), str) else None
    return [auth[f] for f in fields or () if f.endswith("env")]


def _auth_problem(auth, widget_name: str = "") -> str | None:
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
    if missing:
        return f"auth {auth['type']} is missing {', '.join(missing)}"
    if kind == "header" and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", auth["header"]):
        return f"header name {auth['header']!r} is not a plain header name"
    for env in auth_env_names(auth):
        if (problem := env_name_problem(widget_name, env)) is not None:
            return problem
    return None


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
    if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name):
        # ASCII lower-case only: the env allowlist is built by upper-casing the name, and
        # Unicode case mapping turns "ſonarr" into "SONARR".
        problem = "name must be lower-case ASCII letters, digits, '_' and '-'"
    elif not isinstance(match, (list, tuple)) or not match or not all(
            isinstance(entry, str) and entry.strip() for entry in match):
        problem = "match must be a non-empty list of image strings"
    elif (not isinstance(widget.get("port"), int) or isinstance(widget.get("port"), bool)
          or not 0 < widget["port"] < 65536):
        problem = "port must be an int from 1 to 65535"
    elif not isinstance(paths, (list, tuple)) or not paths or not all(
            isinstance(path, str) and _PATH_RE.fullmatch(path) and ".." not in path
            and "//" not in path for path in paths):
        problem = "get must be a non-empty list of rooted paths"
    elif not callable(summarise):
        problem = "no summarise()"
    elif "max_bytes" in widget and (
            not isinstance(widget["max_bytes"], int) or isinstance(widget["max_bytes"], bool)
            or not 0 < widget["max_bytes"] <= MAX_RESPONSE_BYTES_LIMIT):
        problem = f"max_bytes must be an int up to {MAX_RESPONSE_BYTES_LIMIT}"
    else:
        problem = _auth_problem(widget.get("auth"), name)
    if problem is not None:
        log.warning("Skipping widget module %s: %s", module_name, problem)
        return None
    return {**widget, "name": name, "match": list(match), "get": list(paths),
            "summarise": summarise}


def trusted_provenance(widget: dict, pulled_from) -> bool:
    """True when the image carries a registry digest for one of the widget's exact
    (registry, repo) pairs.

    Exact per registry: who owns a namespace differs between Docker Hub and ghcr.io, so a
    ghcr.io/adguard/adguardhome digest is not Docker Hub's adguard/adguardhome.

    Hardening, not proof. With the classic image store a local build has no RepoDigests and
    never matches; with Docker's containerd image store (the default on fresh installs of
    recent Docker) a locally built or `docker tag`ged image gets digests for its tags too, and
    this cannot tell it from a pull. Either way it takes an approved compose or root on the
    host -- inside the trust boundary for widget keys, which is "containers the operator
    approved".
    """
    allowed = {split_image(candidate) for candidate in widget.get("match", ())}
    return any(split_image(repo) in allowed
               for repo in pulled_from or () if isinstance(repo, str))


def widgets_matching(image: str, widgets: dict) -> list:
    """Every widget whose match list names this image's repo, whatever the registry."""
    repo = normalise_image(image)
    return [w for w in widgets.values()
            if repo and repo in {normalise_image(c) for c in w.get("match", ())}]


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
    cleaned = label.strip().lower() if isinstance(label, str) else ""
    if cleaned:
        return None if cleaned == DISABLE else available.get(cleaned)
    matching = widgets_matching(image, available)
    return matching[0] if matching else None
