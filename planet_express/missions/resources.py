"""Canonical resource ids and containment (contract 2.3), shared by grants, protected domains and tripwires.

    host:<h>/stack:<s>   host:<h>/service:<s>/<svc>   host:<h>/mount:<abs>   host:<h>/path:<abs>
    host:<h>/unit:<name> host:<h>/container:<name>    host:<h>/network:<name> host:<h>/system:<name>
    host:<h>/detector:<name>   (a named state detector, equal only to itself)

Containment: `stack:s` contains `service:s/*` (and, given the stacks root, the stack directory); a path or mount
contains every path and mount below it. `classify` is the one matching rule: protected wins.
"""

import posixpath
import re
from collections.abc import Iterable
from typing import Literal

DEFAULT_HOST = "local"
MAX_ID = 512
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$")
_KINDS = ("stack", "service", "mount", "path", "unit", "container", "network", "system", "detector", "remote")
Verdict = Literal["protected", "in_scope", "out_of_scope"]


class BadResource(ValueError):
    """The text is not a resource id."""


def _abs(path: str) -> str:
    if not path.startswith("/") or "\0" in path or ".." in path.split("/"):
        raise BadResource(f"not an absolute normalised path: {path[:80]!r}")
    norm = posixpath.normpath(path)
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def canon(resource: str, host: str = DEFAULT_HOST) -> str:
    """The canonical `host:<h>/<kind>:<name>` form, or BadResource. A bare id gets `host`."""
    if not isinstance(resource, str) or not resource or len(resource) > MAX_ID:
        raise BadResource("resource id must be 1-512 characters")
    if "\0" in resource:
        raise BadResource("resource id contains NUL")
    if resource.startswith("host:"):
        head, sep, rest = resource[5:].partition("/")
        if not sep or not _NAME.match(head):
            raise BadResource(f"bad host in {resource[:80]!r}")
        host, resource = head, rest
    kind, sep, name = resource.partition(":")
    if not sep or kind not in _KINDS or not name:
        raise BadResource(f"unknown resource kind in {resource[:80]!r}")
    if kind in ("path", "mount"):
        name = _abs(name)
        if kind == "mount" and name == "/":
            raise BadResource("the root is not a mount resource")
    elif kind == "service":
        stack, sep, svc = name.partition("/")
        if not sep or not _NAME.match(stack) or not _NAME.match(svc):
            raise BadResource(f"service ids are service:<stack>/<name>: {resource[:80]!r}")
    elif "/" in name or not _NAME.match(name):
        raise BadResource(f"bad name in {resource[:80]!r}")
    return f"host:{host}/{kind}:{name}"


def split(resource: str) -> tuple[str, str, str]:
    """(host, kind, name) of a canonical id."""
    head, _, rest = resource[5:].partition("/")
    kind, _, name = rest.partition(":")
    return head, kind, name


def _under(parent: str, child: str) -> bool:
    return child == parent or child.startswith(parent.rstrip("/") + "/")


def contains(parent: str, child: str, *, stacks_root: str | None = None) -> bool:
    """Does `parent` contain `child` (or equal it)? Both canonical. Different hosts never overlap."""
    if parent == child:
        return True
    ph, pk, pn = split(parent)
    ch, ck, cn = split(child)
    if ph != ch:
        return False
    if pk == "stack":
        if ck == "service":
            return cn.partition("/")[0] == pn
        if ck in ("path", "mount") and stacks_root:
            return _under(posixpath.join(stacks_root, pn), cn)
        return False
    if pk in ("path", "mount") and ck in ("path", "mount"):
        return _under(pn, cn)
    return False


def overlaps(a: str, b: str, *, stacks_root: str | None = None) -> bool:
    return contains(a, b, stacks_root=stacks_root) or contains(b, a, stacks_root=stacks_root)


def classify(resource: str, scope: Iterable[str], protected: Iterable[str], *,
             stacks_root: str | None = None, parent_of_protected: bool = True,
             excluded: Iterable[str] = ()) -> Verdict:
    """Protected wins. By default a resource overlapping a protected one in either direction is protected, because a
    change reported on a parent (a whole mount) may be a change to the protected child. A caller that names a resource
    it only READS (a brokered tool's scope field) passes `parent_of_protected=False`: a stack that merely contains a
    protected service is still readable, the protected service itself is not."""
    protected = list(protected)
    # A grant's own exclusions carve out ONLY the excluded resource and what is inside it: the in-scope parent stays usable.
    if any(contains(e, resource, stacks_root=stacks_root) for e in excluded):
        return "protected"
    hit = (any(overlaps(p, resource, stacks_root=stacks_root) for p in protected) if parent_of_protected
           else any(contains(p, resource, stacks_root=stacks_root) for p in protected))
    if hit:
        return "protected"
    if any(contains(s, resource, stacks_root=stacks_root) for s in scope):
        return "in_scope"
    return "out_of_scope"


__all__ = ["DEFAULT_HOST", "BadResource", "canon", "classify", "contains", "overlaps", "split"]
