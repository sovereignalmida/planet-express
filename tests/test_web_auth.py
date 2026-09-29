import base64
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from itsdangerous import URLSafeSerializer

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import web_auth
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


# ── elevated sessions (T47) ──────────────────────────────────────────────────────

KEY = "k" * 48
TOKEN = "a-device-token"
NOW = 1_800_000_000


CREDENTIAL = "scrypt:32768:8:1$fake$hash"


def _elevate(now=NOW, operator="alice", epoch=0, token=TOKEN, first=None,
             credential=CREDENTIAL):
    return web_auth.make_elevation(KEY, operator, epoch, token, now,
                                   credential=credential, first=first)


def _read(marker, token=TOKEN, now=NOW, credential=CREDENTIAL):
    return web_auth.read_elevation(KEY, marker, token, now, credential=credential)


def test_a_fresh_marker_reads_back():
    assert _read(_elevate(), TOKEN, NOW) == ("alice", 0, NOW)


def test_the_window_runs_out():
    marker = _elevate()
    assert _read(marker, TOKEN, NOW + web_auth.ELEVATION_SECONDS)
    assert _read(marker, TOKEN, NOW + web_auth.ELEVATION_SECONDS + 1) is None


def test_refreshing_slides_the_window_but_not_the_cap():
    """Each elevated action extends the window; the cap is measured from the passphrase entry
    and carried through every refresh, so a long session cannot refresh its way to permanent."""
    first = NOW
    now = NOW
    for _ in range(20):                       # far more refreshes than the cap allows
        now += web_auth.ELEVATION_SECONDS - 1
        marker = _elevate(now=now, first=first)
        got = _read(marker, TOKEN, now)
        if got is None:
            break
    assert now >= first + web_auth.ELEVATION_CAP_SECONDS, "refreshing never hit the cap"
    assert _read(_elevate(now=now, first=first), TOKEN, now) is None


def test_the_cap_is_derived_from_first_not_stored():
    """A marker cannot claim a later `first` than it was minted with without invalidating its
    signature, so the cap cannot be pushed out by re-minting."""
    beyond = NOW + web_auth.ELEVATION_CAP_SECONDS + 1
    marker = _elevate(now=beyond, first=NOW)
    assert _read(marker, TOKEN, beyond) is None


def test_a_marker_is_worthless_on_another_session():
    """Bound to the device token, the way the trust cookie already binds. A marker lifted from
    one browser does nothing in another."""
    assert _read(_elevate(), "a-different-token", NOW) is None


def test_a_marker_from_another_key_is_refused():
    other = web_auth.make_elevation("x" * 48, "alice", 0, TOKEN, NOW, credential=CREDENTIAL)
    assert _read(other, TOKEN, NOW) is None


def test_a_device_token_is_not_an_elevation_marker():
    """Different salts, so one can never be read as the other even though both are signed with
    the same key."""
    token = web_auth.make_device_token(KEY, "alice", 0, NOW)
    assert _read(token, TOKEN, NOW) is None
    assert web_auth.read_device_token(KEY, _elevate(), NOW) is None


@pytest.mark.parametrize("payload", [
    {},
    {"op": "alice", "ep": 0, "tk": "x", "first": NOW},                      # short
    {"op": "alice", "ep": 0, "tk": "x", "first": NOW, "exp": NOW, "x": 1},  # long
    {"op": "", "ep": 0, "tk": None, "first": NOW, "exp": NOW + 60},
    {"op": "ALICE", "ep": 0, "tk": None, "first": NOW, "exp": NOW + 60},
    {"op": "alice", "ep": -1, "tk": None, "first": NOW, "exp": NOW + 60},
    {"op": "alice", "ep": True, "tk": None, "first": NOW, "exp": NOW + 60},   # bool is not int
    {"op": "alice", "ep": 0, "tk": None, "first": "now", "exp": NOW + 60},
    {"op": "alice", "ep": 0, "tk": None, "first": NOW, "exp": "later"},
])
def test_a_payload_that_is_not_exactly_this_shape_is_refused(payload):
    if "tk" in payload and payload["tk"] is None:
        payload["tk"] = web_auth.token_fingerprint(TOKEN)
    if "pw" not in payload and set(payload) >= {"op", "ep", "tk"}:
        payload["pw"] = web_auth.credential_fingerprint(CREDENTIAL)
    forged = URLSafeSerializer(KEY, salt="pe-elevation").dumps(payload)
    assert _read(forged, TOKEN, NOW) is None


def test_a_window_longer_than_we_ever_mint_is_refused():
    """Signed by us, so this is a guard against our own bug rather than a forgery -- a marker
    that somehow carried a year-long window should still not be honoured."""
    forged = URLSafeSerializer(KEY, salt="pe-elevation").dumps({
        "op": "alice", "ep": 0, "tk": web_auth.token_fingerprint(TOKEN),
        "pw": web_auth.credential_fingerprint(CREDENTIAL),
        "first": NOW, "exp": NOW + 365 * 86400})
    assert _read(forged, TOKEN, NOW) is None


def test_a_marker_minted_in_the_future_is_refused():
    assert _read(_elevate(now=NOW + 3600, first=NOW + 3600), TOKEN, NOW) is None


@pytest.mark.parametrize("marker", ["", "not-a-marker", "a.b.c", None, 7, b"bytes"])
def test_rubbish_is_refused(marker):
    assert _read(marker, TOKEN, NOW) is None


def test_the_epoch_is_carried_so_the_caller_can_revoke():
    """read_elevation says the marker is well-formed, not that it is allowed -- the caller
    compares the epoch, which is how a passphrase change revokes every outstanding marker."""
    assert _read(_elevate(epoch=4), TOKEN, NOW) == ("alice", 4, NOW)


def test_a_first_in_the_future_is_refused_on_its_own():
    """The previous test is caught by the window-length check before it reaches the clock-skew
    one. This marker has a window we really would mint and a cap that has not passed, so the
    only thing standing between it and acceptance is `first` being in the future -- which is
    what a backdated clock, or a forgery signed with a leaked key, would look like.
    """
    forged = URLSafeSerializer(KEY, salt="pe-elevation").dumps({
        "op": "alice", "ep": 0, "tk": web_auth.token_fingerprint(TOKEN),
        "pw": web_auth.credential_fingerprint(CREDENTIAL),
        "first": NOW + 3600, "exp": NOW + 10})
    assert _read(forged, TOKEN, NOW) is None


def test_changing_the_passphrase_invalidates_a_marker():
    """The reason a marker carries a credential fingerprint at all.

    `dashboard_operators.py reset` rewrites the credential and asks for a restart; bumping the
    device epoch is a separate command it only *suggests*. Binding to the stored hash -- which
    werkzeug re-salts on every change -- makes the reset revoke outstanding elevations by
    itself, instead of depending on someone also remembering the second step.
    """
    marker = _elevate(credential=hash_passphrase("the old passphrase"))
    assert _read(marker, credential=hash_passphrase("the new passphrase")) is None


def test_the_same_passphrase_set_again_still_invalidates():
    """Re-salted on every write, so even re-setting the same passphrase ends the elevation --
    which is the conservative direction for this to err in."""
    same = "the very same passphrase"
    marker = _elevate(credential=hash_passphrase(same))
    assert _read(marker, credential=hash_passphrase(same)) is None
