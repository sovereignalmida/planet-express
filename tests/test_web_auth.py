import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from itsdangerous import URLSafeSerializer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web_auth import (
    hash_passphrase,
    load_operators,
    make_device_token,
    new_totp_secret,
    provisioning_uri,
    read_device_token,
    totp_at,
    verify_passphrase,
    verify_totp,
)

SECRET = 'GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'


@pytest.mark.parametrize('now,expected', [
    (59, '94287082'), (1111111109, '07081804'), (1111111111, '14050471'),
    (1234567890, '89005924'), (2000000000, '69279037'), (20000000000, '65353130'),
])
def test_rfc_vectors(now, expected):
    assert totp_at(SECRET, now // 30, 8) == expected
    assert totp_at(SECRET.lower(), now // 30) == expected[-6:]


def test_secret_generation_and_padding():
    secret = new_totp_secret()
    assert len(secret) == 32 and secret == secret.upper()
    assert len(base64.b32decode(secret)) == 20
    assert totp_at('MY', 1) == totp_at('my======', 1)
    for bad in ('', '!', 'A', 'é'):
        with pytest.raises(ValueError):
            totp_at(bad, 1)


@pytest.mark.parametrize('offset', [-1, 0, 1])
def test_verify_matched_step(offset):
    assert verify_totp(SECRET, totp_at(SECRET, 100 + offset), 3000) == 100 + offset


def test_verify_rejections():
    for code in ('', '12345', '1234567', 'abcdef', '１２３４５６', None,
                 totp_at(SECRET, 98), totp_at(SECRET, 102)):
        assert verify_totp(SECRET, code, 3000) is None
    assert verify_totp(SECRET, totp_at(SECRET, 99), 3000, window=0) is None
    assert verify_totp(SECRET, totp_at(SECRET, 50), 3000, step_seconds=60) == 50


def test_provisioning_uri():
    uri = urlsplit(provisioning_uri('a/b & c', SECRET, 'Planet & Express'))
    assert uri.scheme == 'otpauth' and uri.netloc == 'totp'
    assert unquote(uri.path) == '/Planet & Express:a/b & c'
    assert parse_qs(uri.query) == {'secret': [SECRET], 'issuer': ['Planet & Express'],
                                 'algorithm': ['SHA1'], 'digits': ['6'], 'period': ['30']}


@pytest.fixture(scope='module')
def password_hash():
    return hash_passphrase('a long passphrase')


def test_passphrases(password_hash):
    assert verify_passphrase(password_hash, 'a long passphrase')
    assert not verify_passphrase(password_hash, 'wrong')
    for bad in ('', 'broken', 'bad$salt$hash', None, 'scrypt:bad$salt$abcd'):
        assert not verify_passphrase(bad, 'a long passphrase')
    with pytest.raises(ValueError):
        hash_passphrase('short')


def test_operators(password_hash):
    env = {'PE_OPERATORS': ' Alice, bob.c-d ', 'PE_OPERATOR_ALICE_PASSPHRASE_HASH': password_hash,
           'PE_OPERATOR_ALICE_TOTP_SECRET': SECRET,
           'PE_OPERATOR_BOB_C_D_PASSPHRASE_HASH': password_hash,
           'PE_OPERATOR_BOB_C_D_TOTP_SECRET': SECRET.lower()}
    operators = load_operators(env)
    assert list(operators) == ['alice', 'bob.c-d']
    assert operators['alice'].passphrase_hash == password_hash
    assert operators['alice'].totp_secret == SECRET
    for variable in ('PE_OPERATOR_ALICE_PASSPHRASE_HASH', 'PE_OPERATOR_ALICE_TOTP_SECRET'):
        for value in (None, 'private-invalid-value'):
            broken = dict(env)
            if value is None:
                del broken[variable]
            else:
                broken[variable] = value
            with pytest.raises(ValueError) as error:
                load_operators(broken)
            assert variable in str(error.value)
            assert SECRET not in str(error.value) and 'private-invalid-value' not in str(error.value)
    for names in ('a,b,c,d', 'a,A', 'a/b', 'a,', '?', 'a' * 33):
        with pytest.raises(ValueError, match='PE_OPERATORS'):
            load_operators({'PE_OPERATORS': names})
    assert load_operators({}) == load_operators({'PE_OPERATORS': ''}) == {}


def test_device_tokens():
    token = make_device_token('key', 'alice', 2, 1000.9)
    assert read_device_token('key', token, 1000) == ('alice', 2)
    assert read_device_token('key', token, 1100, max_age=100) == ('alice', 2)
    assert read_device_token('key', token, 1101, max_age=100) is None
    assert read_device_token('key', token, 939) is None
    assert read_device_token('key', token, 940) == ('alice', 2)
    assert read_device_token('wrong', token, 1000) is None
    assert read_device_token('key', token + 'tamper', 1000) is None
    assert read_device_token('key', 'garbage', 1000) is None
    for payload in ([], {}, {'op': 'alice', 'ep': True, 'iat': 1000},
                    {'op': '?', 'ep': 0, 'iat': 1000}, {'op': 'alice', 'ep': 0, 'iat': True},
                    {'op': 'alice', 'ep': -1, 'iat': 1000}):
        malformed = URLSafeSerializer('key', salt='pe-trusted-device').dumps(payload)
        assert read_device_token('key', malformed, 1000) is None


def test_truncated_password_hash_rejected(password_hash):
    with pytest.raises(ValueError, match='PE_OPERATOR_ALICE_PASSPHRASE_HASH'):
        load_operators({'PE_OPERATORS': 'alice', 'PE_OPERATOR_ALICE_PASSPHRASE_HASH': password_hash[:-2],
                        'PE_OPERATOR_ALICE_TOTP_SECRET': SECRET})


def test_operator_names_with_colliding_variable_prefixes_are_rejected():
    env = {"PE_OPERATORS": "alice-bob,alice.bob"}
    with pytest.raises(ValueError, match="map to the same PE_OPERATOR_ALICE_BOB"):
        load_operators(env)
