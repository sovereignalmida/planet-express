#!/usr/bin/env python3
"""T41 VM rehearsal: the typed planner against real Docker and the real approval path.

Run as casaroot inside the throwaway guest, with casa-planetexpress stopped (this process takes
core's place for the mutation lock):

    sudo systemctl stop casa-planetexpress
    CASA_CONFIG=/etc/planetexpress/config.yaml venv/bin/python tests/homelab/t41-planner.py

The LLM is stubbed — the point is everything after it: server-built bindings from real containers,
the policy/T24 path, the stored plan, and the refusal log. Cases:

  `typed`    a restart plan for unhealthy/web becomes one approval whose stored plan verifies, and
             approving it runs the real container through the engine.
  `recipe`   the vpn.resync_port_forward recipe expands to its five steps with real bindings.
  `refused`  a plan the catalogue cannot express writes planner.refused and sends no card.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import casa_farnsworth as fw
import config
from notifier import FakeNotifier
from planet_express.application import planner
from planet_express.application.command_service import CommandService
from planet_express.core.store import Store
from planet_express.execution import engine

FINDINGS = {"findings": [{"id": "f1", "severity": "HIGH", "resource": "fixture-unhealthy",
                          "description": "unhealthy/web has been unhealthy for 20 minutes"}]}

TYPED = """{"plans": [{"id": "p1", "title": "Restart unhealthy/web", "finding_ids": ["f1"],
  "steps": [{"type": "check.container", "params": {"stack": "unhealthy", "service": "web",
                                                  "expect": "running"}},
            {"type": "service.restart", "params": {"stack": "unhealthy", "service": "web"}}]}]}"""

RECIPE = """{"plans": [{"id": "p1", "title": "Resync the forwarded port", "finding_ids": ["f1"],
  "steps": [{"type": "recipe", "recipe": "vpn.resync_port_forward",
             "params": {"mode": "dead_forward"}}]}]}"""

SHELL = """{"plans": [{"id": "p1", "title": "Remount the share", "finding_ids": ["f1"],
  "steps": [{"type": "shell", "params": {"command": "mount -a"}}]}],
  "needs_human": [{"finding_ids": ["f1"], "why": "fstab is not something I can edit"}]}"""


class State:  # stands in for PipelineState while core is stopped
    busy_reason = "rehearsal"
    mutation_owner = None

    def try_begin_mutation(self, owner, **_):
        return True

    def end_mutation(self, owner, **_):
        pass


def setup():
    store = Store(config.ACTIONS_DB)
    store.init()
    return store, CommandService(store, FakeNotifier(), State())


def reset_attempts():
    """Rehearsal setup only: forget earlier runs against the fixtures so each case starts from a
    clean 24h window instead of waiting out a 30-minute cooldown. The cooldown itself is what the
    `overlap` case proves."""
    store, _ = setup()
    with store._write() as conn:
        conn.execute("DELETE FROM attempts")


def pending(store):
    return store.list_pending()


def refusals(store, since):
    return [e for e in store.list_events()
            if e["kind"] == "planner.refused" and e["ts"] >= since]


def run_case(name, response, *, expect_card):
    store, service = setup()
    notifier = FakeNotifier()
    fw._devise_typed_plans = lambda findings: response  # the LLM's part of the job, stubbed
    before = time.time()
    fw._propose_typed_plans(notifier, FINDINGS, service, store)
    cards = [row for row in pending(store) if row["created_at"] >= before]
    print(f"== {name}: {len(cards)} card(s), {len(refusals(store, before))} refusal(s)")
    for line in notifier.notifications:
        print(f"   notified: {line}")
    if expect_card and cards:
        row = cards[0]
        plan = service._verified_plan(row)
        print(f"   approval {row['id']} risk={row['risk']} plan verifies: {plan is not None}")
        if plan is not None:
            for n, step in enumerate(plan.steps, start=1):
                print(f"     {n}. {step.type} binding={sorted(step.binding)}")
    return store, service, cards


def case_typed():
    store, service, cards = run_case("typed", TYPED, expect_card=True)
    if not cards:
        return
    row = cards[0]
    execution = store.approve_and_create_execution(row["id"], decided_by="t41", arrived_at=time.time())
    plan = service._verified_plan(row)
    result = engine.RunbookEngine(service).run(execution["id"], plan, origin="telegram")
    store.set_execution_status(execution["id"], result.status, reason=result.reason)
    print(f"   run: {result.status} — {result.reason}")
    print(f"   steps: {[(s['n'], s['type'], s['status']) for s in store.list_steps(execution['id'])]}")


def case_recipe():
    # The real gluetun/GSP/qBit trio does not exist in the guest, so the recipe is pointed at the
    # fixture containers: what is being rehearsed is the expansion, the real bindings and the
    # runtime port reference, not the VPN itself.
    planner.VPN_GATEWAY, planner.VPN_SYNC, planner.VPN_CLIENT = (
        "fixture-healthy", "fixture-slow-start", "fixture-crash-loop")
    run_case("recipe", RECIPE, expect_card=True)


def case_refused():
    _, _, cards = run_case("refused", SHELL, expect_card=False)
    assert not cards, "a plan outside the catalogue must never become a card"


def case_overlap():
    """Two different plans for the same service each get a card (their keys are plan hashes), so the
    limits have to hold where the attempts are reserved: the second run must refuse (Codex, T41)."""
    store, service = setup()
    one = """{"plans": [{"id": "p1", "title": "Restart healthy/web", "steps": [
      {"type": "service.restart", "container": "fixture-healthy"}]}]}"""
    two = """{"plans": [{"id": "p2", "title": "Restart healthy/web after a pause", "steps": [
      {"type": "wait", "params": {"seconds": 1}},
      {"type": "service.restart", "container": "fixture-healthy"}]}]}"""
    # Both cards are raised BEFORE either runs — the window where proposal-time checks see no
    # attempt at all and only the reservation can hold the line.
    cards = []
    for raw in (one, two):
        fw._devise_typed_plans = lambda findings, raw=raw: raw
        before = time.time()
        fw._propose_typed_plans(FakeNotifier(), FINDINGS, service, store)
        fresh = [row for row in pending(store) if row["created_at"] >= before]
        if not fresh:
            print(f"   (no card: {[e['payload'].get('reason') for e in refusals(store, before)]})")
        cards += fresh
    print(f"   cards raised: {len(cards)} (want 2)")
    ran = []
    for row in cards:
        execution = store.approve_and_create_execution(row["id"], decided_by="t41",
                                                       arrived_at=time.time())
        result = engine.RunbookEngine(service).run(
            execution["id"], service._verified_plan(row), origin="planner")
        store.set_execution_status(execution["id"], result.status, reason=result.reason)
        ran.append((result.status, result.reason))
        print(f"   steps: {[(x['n'], x['type'], x['status']) for x in store.list_steps(execution['id'])]}")
    print(f"== overlap: {ran}")


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    reset_attempts()
    for name, case in (("typed", case_typed), ("recipe", case_recipe),
                       ("refused", case_refused), ("overlap", case_overlap)):
        if which in (name, "all"):
            case()
