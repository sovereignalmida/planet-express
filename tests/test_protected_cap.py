"""A busy host's protected set (every Docker volume is protected) outgrew the 256-entry list cap and broke the first live
Repair. The set now has its own cap, bounded in practice by the stored grant's size, and every place that carries it
(the store, the brief, the mission detail) copes with hundreds of entries."""
import hashlib

import pytest
from missions_helpers import make_store, new_mission
from test_mission_grants import STACKS, grant

from planet_express.application.mission_service import MAX_PROTECTED_SHOWN, MissionService
from planet_express.missions.brief import DEFAULT_MAX_BYTES, build_brief
from planet_express.missions.grants import with_site_protected
from planet_express.missions.models import GRANT_JSON_MAX, MAX_PROTECTED, Grant
from planet_express.missions.protected import Discovery, ProtectedSet, build_protected
from planet_express.missions.store import MissionStore


def volumes(n: int) -> list[str]:
    return [f"/var/lib/docker/volumes/{hashlib.sha256(str(i).encode()).hexdigest()}/_data" for i in range(n)]


def busy_site(n: int = 902) -> ProtectedSet:
    return build_protected(site=["service:media/traefik"], discovery=Discovery(stacks_root=STACKS, volumes=volumes(n)))


def test_a_grant_carries_the_protected_set_of_a_host_with_902_volumes():
    site = busy_site()
    assert len(site.resources) > 902
    g = with_site_protected(grant(), site)
    assert list(g.protected.resources) == list(site.resources)
    assert len(g.canonical()) <= GRANT_JSON_MAX


def test_a_busy_grant_is_stored_and_read_back_unchanged(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    g = with_site_protected(grant(verification={"checks": ["default"]}), busy_site())
    m = new_mission(ms, g)
    assert ms.get_mission(m.id).grant().protected.resources == g.protected.resources


def test_a_set_too_large_to_store_is_refused_in_words():
    long = [f"/srv/data/{'x' * 300}/{i}" for i in range(1000)]          # ~330 KB of paths
    site = build_protected(site=[], discovery=Discovery(stacks_root=STACKS, volumes=long))
    with pytest.raises(ValueError, match=r"over the 262144 a mission can store; protect a parent path"):
        with_site_protected(grant(), site)


def test_a_set_over_the_entry_cap_is_refused_in_words():
    site = ProtectedSet(host="local", resources=tuple(f"local/path:/v/{i}" for i in range(MAX_PROTECTED + 1)), domains={})
    with pytest.raises(ValueError, match=r"protects 4097 resources, more than the 4096"):
        with_site_protected(grant(), site)


def test_the_model_itself_refuses_more_than_the_cap():
    body = grant().model_dump(mode="json")
    body["protected"] = {"resources": [f"local/path:/v/{i}" for i in range(MAX_PROTECTED + 1)]}
    with pytest.raises(ValueError):
        Grant.model_validate(body)


def test_the_brief_fits_its_cap_and_names_the_broad_entries_first(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    m = new_mission(ms, with_site_protected(grant(verification={"checks": ["default"]}), busy_site()))
    brief = build_brief(m, [], [])
    assert len(brief.text.encode("utf-8")) <= DEFAULT_MAX_BYTES
    line = next(ln for ln in brief.text.splitlines() if ln.startswith("protected (never touch): "))
    assert "local/path:/etc/sudoers" in line and "local/path:/etc/shadow" in line
    assert "more not listed here; every one is still enforced" in line
    assert len(line.encode("utf-8")) < 4096 + 200


def test_a_small_set_is_listed_whole_in_the_brief(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    small = build_protected(site=[], discovery=Discovery(stacks_root=STACKS))
    m = new_mission(ms, with_site_protected(grant(verification={"checks": ["default"]}), small))
    line = next(ln for ln in build_brief(m, [], []).text.splitlines() if ln.startswith("protected (never touch): "))
    assert "more not listed" not in line
    assert all(r.replace(" ", "_") in line for r in small.resources)


def test_the_mission_detail_shows_the_grant_with_the_protected_set_shortened(tmp_path):
    store, _ = make_store(tmp_path)
    ms = MissionStore(store)
    site = busy_site()
    m = new_mission(ms, with_site_protected(grant(verification={"checks": ["default"]}), site))
    got = MissionService(store, enabled=lambda: True).get_mission(m.id)
    assert got["grant"]["tier"] == "repair" and got["grant"]["scope"]["stacks"] == ["media"]
    prot = got["grant"]["protected"]
    assert prot["count"] == len(site.resources) and prot["shortened"] is True
    assert len(prot["resources"]) == MAX_PROTECTED_SHOWN


def test_the_size_check_counts_utf8_bytes_not_characters():
    wide = [f"/srv/data/{'é' * 250}/{i}" for i in range(600)]       # ~160 K characters, ~310 KB of UTF-8
    site = build_protected(site=[], discovery=Discovery(stacks_root=STACKS, volumes=wide))
    with pytest.raises(ValueError, match="a mission can store"):
        with_site_protected(grant(), site)
