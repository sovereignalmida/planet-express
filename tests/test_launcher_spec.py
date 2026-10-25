"""The launch spec: strict, bounded, and identical on both sides (Core's spec.py and the helper's own copy)."""
import json

import pytest
from launcher_support import good_spec, load_helper

from planet_express.missions.launcher.spec import MAX_SPEC_BYTES, LaunchSpec, SpecError, check_wire

helper = load_helper()

BAD = {
    "extra key (a gid)": good_spec(gid=0),
    "extra key (cgroup root)": good_spec(cgroup_root="/sys/fs/cgroup"),
    "missing token": {k: v for k, v in good_spec().items() if k != "token"},
    "uid zero": good_spec(worker_uid=0),
    "uid negative": good_spec(worker_uid=-5),
    "uid bool": good_spec(worker_uid=True),
    "uid string": good_spec(worker_uid="61001"),
    "uid float": good_spec(worker_uid=61001.0),
    "session id slash": good_spec(session_id="a/b"),
    "session id dots": good_spec(session_id=".."),
    "session id empty": good_spec(session_id=""),
    "session id long": good_spec(session_id="x" * 65),
    "relative cwd": good_spec(cwd="work"),
    "nul in cwd": good_spec(cwd="/a\0b"),
    "relative argv0": good_spec(argv=["true"]),
    "argv0 dot": good_spec(argv=["./true"]),
    "empty argv": good_spec(argv=[]),
    "argv not list": good_spec(argv="/bin/true"),
    "argv item int": good_spec(argv=["/bin/true", 1]),
    "argv nul": good_spec(argv=["/bin/true", "a\0b"]),
    "argv too many": good_spec(argv=["/bin/true"] + ["x"] * 64),
    "argv item huge": good_spec(argv=["/bin/true", "x" * 5000]),
    "env not dict": good_spec(env=[["A", "b"]]),
    "env bad name": good_spec(env={"1A": "x"}),
    "env LD_PRELOAD": good_spec(env={"LD_PRELOAD": "/x.so"}),
    "env value nul": good_spec(env={"A": "x\0"}),
    "env value int": good_spec(env={"A": 1}),
    "env too many": good_spec(env={f"V{i}": "x" for i in range(65)}),
    "token short": good_spec(token="abc"),
    "token long": good_spec(token="t" * 257),
    "token space": good_spec(token="t" * 20 + " " + "t" * 20),
    "token non-ascii": good_spec(token="é" * 20),
    "sock_dir not bool": good_spec(sock_dir=1),
    "state_dir not a string": good_spec(state_dir=1),
    "state_dir without the C- prefix": good_spec(state_dir="S-abc"),
    "state_dir empty id": good_spec(state_dir="C-"),
    "state_dir slash": good_spec(state_dir="C-a/b"),
    "state_dir dotdot": good_spec(state_dir="C-.."),
    "state_dir too long": good_spec(state_dir="C-" + "x" * 63),
    "not a dict": ["x"],
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_a_bad_spec_is_refused_by_both_sides(name):
    with pytest.raises(SpecError):
        check_wire(BAD[name])
    with pytest.raises(helper.Refused):
        helper.check_wire(BAD[name])


@pytest.mark.parametrize("spec", [good_spec(), good_spec(sock_dir=True), good_spec(env={}), good_spec(state_dir="C-conv_1-a"),
                                  good_spec(state_dir="C-" + "x" * 62),
                                  good_spec(argv=["/usr/bin/python3", "-c", "print(1)"])])
def test_a_good_spec_is_accepted_by_both_sides(spec):
    assert check_wire(spec) is spec
    assert helper.check_wire(spec) is spec


def test_the_two_bounds_tables_are_the_same():
    from planet_express.missions.launcher import spec as core
    for name in ("MAX_ARGV_ITEMS", "MAX_ARG_CHARS", "MAX_ARGV_CHARS", "MAX_ENV_ITEMS", "MAX_ENV_VALUE_CHARS",
                 "MAX_ENV_CHARS", "MAX_CWD_CHARS", "TOKEN_CHARS", "MAX_SPEC_BYTES", "STATUS_PREFIX"):
        assert getattr(core, name) == getattr(helper, name), name
    assert core.KEYS == helper.SPEC_KEYS and core.REQUIRED == helper.SPEC_REQUIRED
    assert core.STATE_DIR.pattern == helper.STATE_DIR.pattern and core.SESSION_ID.pattern == helper.SESSION_ID.pattern


def test_the_wire_form_never_carries_a_gid_or_a_root():
    wire = json.loads(LaunchSpec(uid=61001, cwd="/w/x", argv=("/bin/true",), token="t" * 40).to_wire("S-1"))
    assert set(wire) == {"session_id", "worker_uid", "cwd", "argv", "env", "token", "sock_dir"}


def test_the_state_dir_is_on_the_wire_only_when_it_is_used():
    used = json.loads(LaunchSpec(uid=61001, cwd="/w/x", argv=("/bin/true",), token="t" * 40, state_dir="C-abc").to_wire("S-1"))
    assert used["state_dir"] == "C-abc"
    with pytest.raises(SpecError):
        LaunchSpec(uid=61001, cwd="/w/x", argv=("/bin/true",), token="t" * 40, state_dir="../etc").to_wire("S-1")


def test_oversize_total_is_refused_before_it_can_block_a_pipe():
    big = {f"V{i}": "x" * 1000 for i in range(40)}
    spec = LaunchSpec(uid=61001, cwd="/w/x", argv=("/bin/true",) + ("y" * 4000,) * 7, env=big, token="t" * 40)
    with pytest.raises(SpecError):
        spec.to_wire("S-1")
    assert MAX_SPEC_BYTES < 65536


def test_the_token_is_not_in_a_repr():
    assert "t" * 40 not in repr(LaunchSpec(uid=1, cwd="/w", argv=("/b",), token="t" * 40))


def test_duplicate_keys_and_json_constants_are_refused_by_the_helper():
    with pytest.raises(helper.Refused):
        helper.parse_json(b'{"a": 1, "a": 2}')
    with pytest.raises(helper.Refused):
        helper.parse_json(b'{"worker_uid": NaN}')
    with pytest.raises(helper.Refused):
        helper.parse_json(b"\xff\xfe")
    with pytest.raises(helper.Refused):
        helper.parse_json(b"[" * 100000)
