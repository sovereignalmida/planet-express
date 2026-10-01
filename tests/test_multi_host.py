"""Guards for T48's cached, observe-only dashboard fleet surface."""

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config_schema import HostEntry, MultiHostConfig
from planet_express.application.multi_host import (
    FIXTURE_NOW,
    REFRESH_PENDING,
    FleetCache,
    provider_for,
)
from planet_express.core.hosts import CURRENT, Host, Liveness, RemoteContainer, unknown
from planet_express.integrations.beszel import (
    BeszelHubProvider,
    ContainerReading,
    FixtureHostProvider,
    FleetReading,
    HostReading,
)

LOCAL = "local-system"
REMOTE = "remote-system"
UNCONFIGURED = "new-system"
LOCAL_NAMES = ("a", "b", "c", "d", "e")


class Provider:
    def __init__(self, *, readable=True, include_unconfigured=False):
        self.readable = readable
        self.include_unconfigured = include_unconfigured

    def hosts(self):
        if not self.readable:
            return FleetReading(unknown("the collector is unreachable"))
        rows = [
            HostReading(Host(LOCAL, "editable local name", status="up"), liveness=Liveness(CURRENT)),
            HostReading(Host(REMOTE, "editable remote name", status="up"), liveness=Liveness(CURRENT)),
        ]
        if self.include_unconfigured:
            rows.append(HostReading(Host(UNCONFIGURED, "new collector row", status="up"),
                                   liveness=Liveness(CURRENT)))
        return FleetReading(Liveness(CURRENT), tuple(rows))

    def containers(self, system_id):
        names = {LOCAL: LOCAL_NAMES, REMOTE: ("beszel-agent",), UNCONFIGURED: ("new",)}
        return ContainerReading(system_id, Liveness(CURRENT),
                                tuple(RemoteContainer(name) for name in names[system_id]))


def inventory(*, pin=None):
    return MultiHostConfig(hosts=[HostEntry(system_id=REMOTE, name="Configured remote", link=None)],
                           local_system_id=pin)


def test_derived_local_system_is_excluded_even_if_collector_name_changes():
    view = FleetCache(inventory(), Provider(), lambda: LOCAL_NAMES)._collect()
    assert [card.id for card in view.cards] == [REMOTE]
    assert view.locality_id == LOCAL


def test_fixture_is_the_default_provider_and_uses_a_frozen_capture_clock():
    fixture = provider_for(lambda: ("", ""))
    assert isinstance(fixture, FixtureHostProvider)
    assert fixture._wall() == FIXTURE_NOW
    assert isinstance(provider_for(lambda: ("reader", "password")), BeszelHubProvider)


def test_configured_hosts_render_unknown_when_collector_is_unreadable():
    view = FleetCache(inventory(), Provider(readable=False), lambda: LOCAL_NAMES)._collect()
    card, = view.cards
    assert card.id == REMOTE
    assert card.liveness.state == "unknown"
    assert card.liveness.reason == "the collector is unreachable"
    assert card.containers is None


def test_unconfigured_collector_rows_are_withheld_until_locality_is_known():
    # A monitor that has not collected containers is unknown, not an empty local Docker set.
    view = FleetCache(inventory(), Provider(include_unconfigured=True), lambda: None)._collect()
    assert [card.id for card in view.cards] == [REMOTE]
    assert view.locality_id is None
    assert "withheld" in view.locality_reason


def test_current_stale_unknown_and_zero_containers_remain_distinct():
    provider = Provider()
    cache = FleetCache(inventory(), provider, lambda: LOCAL_NAMES)
    view = cache._collect()
    assert view.cards[0].liveness.state == "current"
    assert view.cards[0].containers == (RemoteContainer("beszel-agent"),)

    class ZeroProvider(Provider):
        def containers(self, system_id):
            if system_id == REMOTE:
                return ContainerReading(system_id, Liveness(CURRENT), ())
            return super().containers(system_id)

    zero = FleetCache(inventory(), ZeroProvider(), lambda: LOCAL_NAMES)._collect().cards[0]
    assert zero.containers == ()
    stale = HostReading(Host(REMOTE), liveness=Liveness("stale", "old", 121))
    assert stale.liveness.state == "stale"  # a stale state is not folded into unknown.


def test_slow_provider_never_blocks_a_hosts_render():
    started = threading.Event()
    release = threading.Event()

    class SlowProvider(Provider):
        def hosts(self):
            started.set()
            release.wait(2)
            return super().hosts()

    cache = FleetCache(inventory(), SlowProvider(), lambda: LOCAL_NAMES)
    before = time.monotonic()
    first = cache.view()
    elapsed = time.monotonic() - before
    assert elapsed < 0.1
    assert first.cards[0].liveness.reason == REFRESH_PENDING
    assert started.wait(1)
    release.set()


def test_a_pin_unblocks_unconfigured_rows_when_docker_evidence_is_missing():
    """The gate is locality-KNOWN, not locality-DERIVED.

    A pin exists precisely for hosts where docker evidence is absent or ambiguous. Gating the
    unconfigured rows on the derivation alone withheld them on exactly those hosts: the pin
    established locality and the rows stayed hidden anyway.
    """
    view = FleetCache(inventory(pin=LOCAL), Provider(include_unconfigured=True),
                      lambda: None)._collect()
    assert view.locality_id == LOCAL
    assert view.locality_reason is None
    ids = [card.id for card in view.cards]
    assert LOCAL not in ids, "the pinned local system is still excluded from remote entries"
    assert UNCONFIGURED in ids, "a pin makes locality known, so the rows are no longer withheld"


def test_with_neither_derivation_nor_pin_the_rows_stay_withheld():
    view = FleetCache(inventory(), Provider(include_unconfigured=True), lambda: None)._collect()
    assert view.locality_id is None
    assert UNCONFIGURED not in [card.id for card in view.cards], \
        "locality unknown: an unconfigured row could be this host about to render twice"
