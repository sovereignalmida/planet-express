"""Resource ids, containment and the protected-wins classification (contract 2.3, S6)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.missions.resources import (
    BadResource,
    canon,
    classify,
    contains,
    overlaps,
)

H = "host:local/"


def test_canon_adds_host_and_normalises():
    assert canon("stack:media") == H + "stack:media"
    assert canon(H + "path:/a/b/") == H + "path:/a/b"
    assert canon("host:nas/path:/x") == "host:nas/path:/x"


@pytest.mark.parametrize("bad", ["", "stack:", "bogus:x", "path:rel", "path:/a/../b", "service:nostack",
                                 "path:/a\0b", "stack:a/b", "mount:/", "x" * 600])
def test_canon_refuses_malformed(bad):
    with pytest.raises(BadResource):
        canon(bad)


def test_stack_contains_its_services_and_stack_paths():
    assert contains(canon("stack:s"), canon("service:s/web"))
    assert not contains(canon("stack:s"), canon("service:t/web"))
    assert not contains(canon("service:s/web"), canon("stack:s"))
    assert contains(canon("stack:s"), canon("path:/srv/stacks/s/x.yml"), stacks_root="/srv/stacks")
    assert not contains(canon("stack:s"), canon("path:/srv/stacks/sx/x.yml"), stacks_root="/srv/stacks")


def test_mount_and_path_containment():
    assert contains(canon("mount:/a"), canon("path:/a/b/c"))
    assert contains(canon("mount:/a"), canon("mount:/a/b"))
    assert contains(canon("path:/a"), canon("path:/a/b"))
    assert not contains(canon("path:/a"), canon("path:/ab"))
    assert not contains(canon("path:/a/b"), canon("path:/a"))
    assert contains(canon("path:/a"), canon("mount:/a/b"))


def test_hosts_never_overlap():
    assert not contains(canon("path:/a"), canon("host:nas/path:/a/b"))
    assert not overlaps(canon("stack:s"), canon("host:nas/stack:s"))


def test_detectors_only_equal_themselves():
    assert contains(canon("detector:boot"), canon("detector:boot"))
    assert not contains(canon("detector:boot"), canon("detector:packages"))


def test_classify_protected_wins_inside_a_stack():
    scope = [canon("stack:media")]
    protected = [canon("service:media/traefik")]
    assert classify(canon("service:media/qbit"), scope, protected) == "in_scope"
    assert classify(canon("service:media/traefik"), scope, protected) == "protected"
    assert classify(canon("service:other/x"), scope, protected) == "out_of_scope"


def test_classify_protected_wins_over_a_scope_path():
    scope = [canon("path:/srv/stacks/media")]
    protected = [canon("path:/srv/stacks/media/traefik")]
    assert classify(canon("path:/srv/stacks/media/traefik/dyn.yml"), scope, protected) == "protected"
    assert classify(canon("path:/srv/stacks/media/app.yml"), scope, protected) == "in_scope"


def test_classify_a_parent_of_a_protected_path_is_protected():
    # a change reported at /mnt/data may be a change to /mnt/data/photos: ambiguity fails closed
    assert classify(canon("path:/mnt/data"), [canon("path:/mnt/data")], [canon("path:/mnt/data/photos")]) == "protected"
