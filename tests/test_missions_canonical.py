import hashlib

import pytest
from missions_helpers import repair_grant  # noqa: F401  (also sets CASA_CONFIG)
from pydantic import ValidationError

from planet_express.missions.canonical import (
    CanonicalError,
    canonical_json,
    loads_strict,
    sha256_hex,
)
from planet_express.missions.models import Grant, VerificationPlan


def test_key_order_and_whitespace_independent():
    a = loads_strict('{"b": 1, "a": [true, null, "x"]}')
    b = loads_strict('{\n"a":[true,null,"x"],\n\t"b":1}')
    assert canonical_json(a) == canonical_json(b) == '{"a":[true,null,"x"],"b":1}'
    assert sha256_hex(a) == sha256_hex(b)


def test_stable_vectors():
    assert canonical_json({"é": "é", "a": "\u0001\n"}) == '{"a":"\\u0001\\n","é":"é"}'
    assert canonical_json([]) == "[]" and canonical_json({}) == "{}"
    vector = {"b": 2, "a": 1}
    assert sha256_hex(vector) == hashlib.sha256(b'{"a":1,"b":2}').hexdigest()
    assert sha256_hex(vector) == "43258cff783fe7036d8a43033f830adfc60ec037382473548ac742b888292777"


def test_utf16_key_order():
    # U+FF5E sorts after U+1F600 in code points but before it in UTF-16 code units
    assert canonical_json({"～": 1, "\U0001f600": 2}) == '{"\U0001f600":2,"～":1}'


@pytest.mark.parametrize("bad", ['{"a":1.5}', '{"a":1e2}', '{"a":NaN}', '{"a":Infinity}', '{"a":-Infinity}',
                                 '{"a":1,"a":2}', '{"x":{"a":1,"a":1}}', '{"a":9007199254740993}', "{bad"])
def test_loads_rejects(bad):
    with pytest.raises(CanonicalError):
        loads_strict(bad)


def test_rejects_non_utf8_and_lone_surrogates():
    with pytest.raises(CanonicalError):
        loads_strict(b'{"a":"\xff"}')
    with pytest.raises(CanonicalError):
        canonical_json({"a": "\ud800"})


@pytest.mark.parametrize("bad", [1.5, float("nan"), float("inf"), {"a": 0.1}, [1.0], {1: 2}, object()])
def test_canonical_rejects_floats_and_foreign_types(bad):
    with pytest.raises(CanonicalError):
        canonical_json(bad)


def test_models_refuse_floats_and_extra_keys():
    with pytest.raises(ValidationError):
        Grant.model_validate({"tier": "repair", "budgets": {"wall_s": 3600.0}})
    with pytest.raises(ValidationError):
        Grant.model_validate({"tier": "repair", "surprise": 1})
    with pytest.raises(ValidationError):
        Grant.model_validate({"tier": "godmode"})
    with pytest.raises(ValidationError):
        Grant.model_validate({"tier": "repair", "docker": "raw"})  # raw needs broad sudo
    Grant.model_validate({"tier": "repair", "docker": "raw", "sudo": {"mode": "broad"}})
    with pytest.raises(ValidationError):
        VerificationPlan.model_validate({"checks": [{"id": "a", "check": "http", "params": {"t": 1.5}}]})


def test_grant_hash_stable_and_sensitive():
    g1 = Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media"]}})
    g2 = Grant.model_validate({"scope": {"stacks": ["media"]}, "tier": "repair"})
    g3 = Grant.model_validate({"tier": "repair", "scope": {"stacks": ["media2"]}})
    assert g1.hash() == g2.hash() != g3.hash()
    assert g1.canonical() == canonical_json(g1.model_dump(mode="json"))
