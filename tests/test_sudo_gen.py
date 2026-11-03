"""The generated worker sudoers file (slice 7b): exact text, no wildcard by construction, visudo, the setup step (preview,
probe, repair, undo, removal) and the side-door check that recognises exactly this output as managed."""
import os
import random
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.missions.launcher.side_doors import (
    SideDoorPaths,
    SudoersManaged,
)
from planet_express.missions.sudo_gen import (
    SudoGenError,
    digest,
    normalise,
    outside_ceiling,
    render,
)

RULES = [("plex.service", ("restart", "start")), ("a@b.timer", ("stop",))]
SNAPSHOT = """# Managed by Planet Express (worker sudo scope). Do not edit: setup rewrites it and the side-door check hashes it.
User_Alias PE_SESSION_UIDS_0 = #61000, #61001, #61002, #61003, #61004, #61005, #61006, #61007, #61008, #61009, #61010, #61011, #61012, #61013, #61014, #61015, \\
    #61016, #61017, #61018, #61019, #61020, #61021, #61022, #61023, #61024, #61025, #61026, #61027, #61028, #61029, #61030, #61031, \\
    #61032, #61033, #61034, #61035, #61036, #61037, #61038, #61039
User_Alias PE_SESSIONS = PE_SESSION_UIDS_0
Cmnd_Alias PE_WORKER_CMDS = /usr/bin/systemctl stop a@b.timer, \\
    /usr/bin/systemctl start plex.service, \\
    /usr/bin/systemctl restart plex.service
Defaults:PE_SESSIONS env_reset, !setenv, !env_editor
Defaults!/usr/bin/systemctl env_reset, !setenv, !env_editor
PE_SESSIONS ALL = (root) NOPASSWD: PE_WORKER_CMDS
"""
CMD = re.compile(r"^/usr/bin/systemctl (start|stop|restart) [A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\.(service|socket|timer|mount|path|target)$")


def test_the_text_is_exactly_this():
    assert render(RULES, uid_min=61000, uid_max=61040).decode() == SNAPSHOT


def test_no_rules_means_no_file_and_the_same_rules_always_render_the_same_bytes():
    assert render([], uid_min=61000, uid_max=61999) is None
    a = render(RULES, uid_min=61000, uid_max=61999)
    b = render(list(reversed(RULES)) + [("plex.service", ("start",))], uid_min=61000, uid_max=61999)
    assert a == b and digest(a) == digest(b)


def test_the_top_uid_is_never_named_and_a_huge_range_is_refused():
    text = render(RULES, uid_min=61000, uid_max=61999).decode()
    assert "#61998" in text and "#61999" not in text
    with pytest.raises(SudoGenError):
        render(RULES, uid_min=1000, uid_max=1000 + 5000)
    with pytest.raises(SudoGenError):
        render(RULES, uid_min=0, uid_max=100)


HOSTILE = ["*.service", "a b.service", "x.service\nroot ALL=(ALL) NOPASSWD: ALL", "ALL", "ALL.service", "a,b.service", "a:b.service",
           "a\\.service", "!x.service", "#x.service", "x.service ", "", "x", "../x.service", "x.service/y", "a=b.service",
           "x.service; id", "$(id).service", "é.service", "x" * 200 + ".service", ".service", "x.service\x00", "x.slice", "[a].service",
           "a?.service", "-x.service", "x.service,y.service", "%wheel.service", "x.service\\", "'x'.service", '"x".service']


@pytest.mark.parametrize("unit", HOSTILE)
def test_a_hostile_unit_name_is_refused_never_rendered(unit):
    with pytest.raises(SudoGenError):
        render([(unit, ("restart",))], uid_min=61000, uid_max=61010)


@pytest.mark.parametrize("action", ["reload", "restart *", "RESTART", "", "start stop", "enable", "kill", "daemon-reload", None])
def test_only_start_stop_restart_are_actions(action):
    with pytest.raises(SudoGenError):
        render([("x.service", (action,))], uid_min=61000, uid_max=61010)
    with pytest.raises(SudoGenError):
        render([("x.service", ())], uid_min=61000, uid_max=61010)


def test_property_whatever_is_rendered_is_exactly_the_listed_argv_and_nothing_wild():
    rng = random.Random(7)
    alphabet = list("abcxyz019_.@-*?[]\\!#,: \n\t$%=ALL") + ["ALL", ".service", ".timer", "é"]
    rendered = 0
    for _ in range(3000):
        safe = list("abcxyz019_.@-")
        units = ["".join(rng.choice(safe if rng.random() < 0.9 else alphabet) for _ in range(rng.randint(1, 12)))
                 + rng.choice(["", ".service", ".mount", ".x", ".timer", ".timer"]) for _ in range(rng.randint(1, 4))]
        acts = [rng.sample(["start", "stop", "restart", "restart", "start", "reload", "x *", "ALL"], rng.randint(1, 3)) for _ in units]
        try:
            out = render(list(zip(units, acts, strict=True)), uid_min=61000, uid_max=61020)
        except SudoGenError:
            continue
        rendered += 1
        text = out.decode("ascii")
        body = text.replace("\\\n", "")
        cmds = re.search(r"Cmnd_Alias PE_WORKER_CMDS = (.*)", body).group(1).split(", ")
        assert cmds and all(CMD.fullmatch(c.strip()) for c in cmds), cmds
        want = {f"/usr/bin/systemctl {a} {u}" for u, ac in zip(units, acts, strict=True) for a in ac}
        assert {c.strip() for c in cmds} == want
        for line in body.splitlines():
            if line.startswith(("Cmnd_Alias", "User_Alias")):
                assert not any(ch in line for ch in "*?[]!;$|&`<>\\\"'"), line
        assert len(re.findall(r"\bALL\b", body)) == 1 and "ALL = (root) NOPASSWD" in body
        assert "authenticate" not in body and "env_keep" not in body and "SETENV" not in body
    assert rendered > 50            # the property was exercised, not vacuous


@pytest.mark.skipif(shutil.which("visudo") is None and not Path("/usr/sbin/visudo").exists(), reason="no visudo here")
def test_visudo_accepts_the_generated_file(tmp_path):
    visudo = shutil.which("visudo") or "/usr/sbin/visudo"
    probe = tmp_path / "probe"
    probe.write_text("Defaults env_reset\n")
    if subprocess.run([visudo, "-c", "-f", str(probe)], capture_output=True, check=False).returncode != 0:
        pytest.skip("visudo cannot check a plain file as this user")
    path = tmp_path / "worker"
    path.write_bytes(render(RULES, uid_min=61000, uid_max=61999))
    done = subprocess.run([visudo, "-c", "-f", str(path)], capture_output=True, check=False)
    assert done.returncode == 0, done.stderr


def test_a_grant_is_checked_against_the_host_ceiling():
    ceiling = [("plex.service", ("start", "restart"))]
    ok = SimpleNamespace(unit="plex.service", actions=["restart"])
    more = SimpleNamespace(unit="plex.service", actions=["stop"])
    other = SimpleNamespace(unit="sonarr.service", actions=["start"])
    assert outside_ceiling(ceiling, [ok]) == []
    assert len(outside_ceiling(ceiling, [more, other])) == 2
    assert len(outside_ceiling([], [ok])) == 1


def test_normalise_accepts_dicts_pairs_and_objects():
    got = normalise([{"unit": "a.service", "actions": ["start"]}, ("a.service", ["stop"]), SimpleNamespace(unit="b.timer", actions=("start",))])
    assert [(r.unit, r.actions) for r in got] == [("a.service", ("start", "stop")), ("b.timer", ("start",))]


# ── the setup step ──────────────────────────────────────────────────────────────────────────────────
# Retired with trust mode: setup no longer writes a worker sudoers ceiling (one session rule, `session-access`, instead). The
# generator stays until stage 4 removes scoped sudo; its text is still what the side-door check treats as managed when present.

# ── the side-door check ─────────────────────────────────────────────────────────────────────────────

def side_doors(tmp_path, extra=None):
    etc = tmp_path / "etc"
    (etc / "sudoers.d").mkdir(parents=True)
    (etc / "passwd").write_text("root:x:0:0::/root:/bin/sh\nplanetexpress:x:1001:1001::/home/pe:/bin/sh\n")
    (etc / "group").write_text("root:x:0:\nplanetexpress:x:1001:\n")
    (etc / "sudoers").write_text(f"Defaults env_reset\nroot ALL=(ALL:ALL) ALL\n@includedir {etc}/sudoers.d\n")
    target = etc / "sudoers.d" / "planetexpress-worker"
    target.write_bytes(render(RULES, uid_min=61000, uid_max=61999))
    managed = {str(target): render(RULES, uid_min=61000, uid_max=61999)}
    return target, SideDoorPaths(
        core_user="planetexpress", uid_min=61000, uid_max=61999, passwd=str(etc / "passwd"), group=str(etc / "group"),
        sudoers=str(etc / "sudoers"), sudoers_dir=str(etc / "sudoers.d"), managed={**managed, **(extra or {})},
        run=lambda *a, **k: SimpleNamespace(returncode=0, stdout=b"", stderr=b""), owner_uid=os.geteuid(), owner_gid=os.getegid())


def test_the_check_accepts_exactly_the_generated_file(tmp_path):
    target, p = side_doors(tmp_path)
    probe = SudoersManaged(p).probe()
    assert probe.ok, probe.detail


def test_the_check_rejects_an_altered_generated_file(tmp_path):
    target, p = side_doors(tmp_path)
    target.write_text(target.read_text().replace("restart plex.service", "restart plex.service, /usr/bin/systemctl restart *"))
    probe = SudoersManaged(p).probe()
    assert not probe.ok and "differs" in probe.detail
    target.write_bytes(render(RULES, uid_min=61000, uid_max=61999))
    assert SudoersManaged(p).probe().ok
    target.write_bytes(render(RULES, uid_min=61000, uid_max=61999) + b"\n")
    assert not SudoersManaged(p).probe().ok


def test_the_same_text_anywhere_else_is_not_managed(tmp_path):
    target, p = side_doors(tmp_path)
    (tmp_path / "etc" / "sudoers.d" / "copy").write_bytes(target.read_bytes())
    assert not SudoersManaged(p).probe().ok


def test_the_generated_rules_would_be_flagged_if_they_were_not_declared_managed(tmp_path):
    target, p = side_doors(tmp_path)
    p.managed = {}
    assert not SudoersManaged(p).probe().ok


# ── clearing the rules must not leave the old grants in force (review finding) ──────────────────────────────

def test_reparse_recognises_exactly_generator_output_whatever_rules_it_held():
    from planet_express.missions.sudo_gen import is_generated, reparse
    text = render(RULES, uid_min=61000, uid_max=61999)
    rules, low, high = reparse(text)
    assert [(r.unit, r.actions) for r in rules] == [("a@b.timer", ("stop",)), ("plex.service", ("start", "restart"))]
    assert (low, high) == (61000, 61999) and is_generated(text)
    assert not is_generated(text + b"# edited\n") and not is_generated(b"") and not is_generated(b"root ALL=(ALL) ALL\n")
    assert not is_generated(text.replace(b"restart plex.service", b"restart plex.service, /bin/sh"))
    assert not is_generated(text.replace(b"/usr/bin/systemctl stop", b"/usr/bin/systemctl stop *"))
