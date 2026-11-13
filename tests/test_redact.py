"""Secret patterns and the immutable configuration snapshot, without host I/O."""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from planet_express.core.redact import MAX_REDACT_CHARS, REDACTED, redact


@pytest.mark.parametrize(("text", "expected"), [
    ("API_KEY=abc123", "API_KEY=[REDACTED]"),
    ("PASSWORD=hunter2", "PASSWORD=[REDACTED]"),
    ("MY_SECRET_TOKEN=xyz", "MY_SECRET_TOKEN=[REDACTED]"),
    ("Authorization: Bearer abc.def", "Authorization: Bearer [REDACTED]"),
    ("authorization: bEaReR abc.def", "authorization: bEaReR [REDACTED]"),
    ("https://user:pw@example.com/x", "https://[REDACTED]@example.com/x"),
    # Token-boundary design: a value that opens a quote the token cannot prove is closed
    # is withheld whole, quote included. Structure-preserving output ('PASSWORD="[REDACTED]"')
    # was the previous design and is what leaked on escaped quotes and Bearer prefixes.
    ('PASSWORD="two words"', "PASSWORD=[REDACTED]"),
    ("TOKEN='two words'", "TOKEN=[REDACTED]"),
    ("KEYBOARD=us PASSAGE=3 MONKEY=1", "KEYBOARD=us PASSAGE=3 MONKEY=1"),
])
def test_patterns_and_idempotence(text, expected):
    assert redact(text) == expected
    assert redact(redact(text)) == expected


def test_multiline():
    assert redact("API_KEY=one\nPASSWORD=two\nAPI_KEY=three") == (
        "API_KEY=[REDACTED]\nPASSWORD=[REDACTED]\nAPI_KEY=[REDACTED]"
    )


def test_oversized_input_is_withheld():
    assert redact("API_KEY=planted\n" + "x" * MAX_REDACT_CHARS) == REDACTED


def _snapshot(monkeypatch, values):
    calls = []
    for name, value in values.items():
        def accessor(name=name, value=value):
            calls.append(name)
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(config, name, accessor)
    # Isolated module avoids changing the snapshot used by other tests/call sites.
    spec = importlib.util.spec_from_file_location(
        "isolated_redact", Path(__file__).parents[1] / "planet_express/core/redact.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module, calls


def test_literals_snapshot_and_marker_protection(monkeypatch):
    module, calls = _snapshot(monkeypatch, {
        "anthropic_api_key": "provider-secret",
        "openai_api_key": "REDACTED",
        "telegram_credentials": ("bot-secret", "chat-secret"),
        "adguard_credentials": ("adguard-user", "adguard-password"),
    })
    text = "provider-secret bot-secret chat-secret adguard-user adguard-password"
    assert module.redact(text) == " ".join([REDACTED] * 5)
    assert module.redact(module.redact(text)) == module.redact(text)
    assert module.redact("API_KEY=provider-secret") == "API_KEY=[REDACTED]"
    assert len(calls) == 4


def test_missing_empty_values(monkeypatch):
    module, calls = _snapshot(monkeypatch, {
        "anthropic_api_key": RuntimeError("missing"),
        "openai_api_key": "",
        "telegram_credentials": RuntimeError("missing"),
        "adguard_credentials": ("", ""),
    })
    assert module.redact("ordinary output") == "ordinary output"
    assert module.redact("") == ""
    assert len(calls) == 4


def test_literal_inside_bearer_still_removes_entire_token(monkeypatch):
    module, _ = _snapshot(monkeypatch, {
        "anthropic_api_key": "prefix",
        "openai_api_key": "",
        "telegram_credentials": ("", ""),
        "adguard_credentials": ("", ""),
    })
    assert module.redact("Bearer prefix.suffix") == "Bearer [REDACTED]"


# ── second-review finding (T18, gate 13): spans found on the original, unioned ────
def _only_adguard(monkeypatch, user, password):
    module, _ = _snapshot(monkeypatch, {
        "anthropic_api_key": "", "openai_api_key": "",
        "telegram_credentials": ("", ""), "adguard_credentials": (user, password),
    })
    return module


def test_configured_value_equal_to_a_scheme_does_not_disable_the_bearer_pass(monkeypatch):
    module = _only_adguard(monkeypatch, "", "Bearer")
    out = module.redact("Bearer unknown-service-token")     # gate 13, verbatim
    assert "unknown-service-token" not in out, out
    assert module.redact(out) == out


@pytest.mark.parametrize(("user", "password", "text", "fragments"), [
    ("admin", "min-secret", "login failed: admin-secret", ("-secret", "min")),   # gate 13
    ("aaaa", "aaab", "xaaaaabx", ("aaaa", "aaab", "b")),
])
def test_overlapping_configured_values_are_masked_as_a_union(
    monkeypatch, user, password, text, fragments,
):
    module = _only_adguard(monkeypatch, user, password)
    out = module.redact(text)
    for fragment in fragments:
        assert fragment not in out.replace(REDACTED, ""), out
    assert module.redact(out) == out


def test_configured_value_inside_the_marker_does_not_damage_it(monkeypatch):
    module = _only_adguard(monkeypatch, "RED", "ACT")
    for text in ("PASSWORD=[REDACTED]", "Authorization: Bearer [REDACTED]"):
        assert module.redact(text) == text


# ── second-review finding (T18, gate 12): a pattern pass must not split a literal ──
@pytest.mark.parametrize("text", [
    "login failed for credential Bearer abc,remaining-secret",   # gate 12, verbatim
    "PASSWORD=Bearer abc,remaining-secret",
    "x Bearer abc,remaining-secret PASSWORD=y",
    "Authorization: Bearer abc,remaining-secret",
])
def test_configured_value_containing_a_pattern_is_masked_whole(monkeypatch, text):
    module, _ = _snapshot(monkeypatch, {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "telegram_credentials": ("", ""),
        "adguard_credentials": ("admin", "Bearer abc,remaining-secret"),
    })
    out = module.redact(text)
    assert "remaining-secret" not in out and "abc," not in out, out
    assert module.redact(out) == out, out


# ── second-review finding (T18, gate 11): literal pass must not hide a key ────────
@pytest.mark.parametrize("configured", ["token", "pass", "key", "secret", "access"])
def test_configured_value_inside_a_key_name_does_not_hide_the_key(monkeypatch, configured):
    module, _ = _snapshot(monkeypatch, {
        "anthropic_api_key": "",
        "openai_api_key": "",
        "telegram_credentials": ("", ""),
        "adguard_credentials": ("admin", configured),
    })
    for text in ("access_token=unconfigured-service-credential",
                 "DB_PASSWORD=unconfigured-service-credential",
                 '{"api_key":"unconfigured-service-credential"}',
                 "client_secret: unconfigured-service-credential"):
        out = module.redact(text)
        assert "unconfigured-service-credential" not in out, out
        assert module.redact(out) == out, out


# ── second-review finding (T18): punctuation must not truncate redaction ──────────
@pytest.mark.parametrize("text", [
    "PASSWORD=abc,remaining-secret",
    "PASSWORD=a;b;c",
    "DB_PASSWORD=p@ss,w0rd;x",
    "API_KEY=sk-live-a,b,c",
    "TOKEN=x'y,z",
    'SECRET=a"b,c',
])
def test_secret_values_with_punctuation_are_fully_redacted(text):
    """Commas/semicolons/quotes are legal in passwords; redacting only the prefix
    published the tail into planner evidence and the step log."""
    out = redact(text)
    key = text.split("=", 1)[0]
    assert out == f"{key}={REDACTED}", out
    for fragment in ("remaining-secret", "b;c", "w0rd", "sk-live-a", "y,z", 'b,c'):
        assert fragment not in out


@pytest.mark.parametrize("text,expected", [
    ('PASSWORD="quoted,secret" trailing=ok', f"PASSWORD={REDACTED}"),
    ("PASSWORD='single,quoted' KEYBOARD=us", f"PASSWORD={REDACTED}"),
    # The withhold stops at the LINE, so the next line is untouched.
    ('PASSWORD="q,s" t=ok\nB=2', f"PASSWORD={REDACTED}\nB=2"),
    # Escaped quotes need no special handling now: the value's end is never computed.
    ('PASSWORD="abc\\" remaining-secret"', f"PASSWORD={REDACTED}"),
    # JSON keys are quoted, so the key does not touch its separator.
    ('{"PASSWORD":"s3cret"}', f'{{"PASSWORD":{REDACTED}'),
    # A marker in the value must not exempt the rest of the token from redaction.
    ("PASSWORD=[REDACTED],TOKEN=unknown-secret", f"PASSWORD={REDACTED}"),
])
def test_sensitive_key_withholds_the_rest_of_its_line(text, expected):
    """Everything after a sensitive key goes, whatever shape it has. Deliberate
    over-redaction: `trailing=ok` and `KEYBOARD=us` are innocent and still lost. Six
    review rounds of computing where a value ENDS leaked seven ways (punctuation, quotes,
    escaped quotes, JSON, bare colons, a Bearer prefix, a marker exemption), so the end is
    no longer computed at all."""
    assert redact(text) == expected
    for fragment in ("quoted", "secret", "single", "q,s", "remaining", "unknown"):
        assert fragment not in redact(text)


def test_innocent_keys_with_punctuation_are_untouched():
    text = "KEYBOARD=us,intl PASSAGE=3;4 MONKEY=a,b"
    assert redact(text) == text


@pytest.mark.parametrize("text,expected", [
    # A non-secret key must not swallow a later secret on the same line: an
    # end-of-line value group made `A=1 PASSWORD=x,y B=2` pass through untouched.
    # Content BEFORE the sensitive key survives; content after it does not.
    ("A=1 PASSWORD=x,y B=2", f"A=1 PASSWORD={REDACTED}"),
    ("HOST=h API_KEY=sk,live PORT=80", f"HOST=h API_KEY={REDACTED}"),
    ("TOKEN=t1,t2 PASSWORD=p1;p2", f"TOKEN={REDACTED}"),
    ("A=1 PASSWORD=abc,remaining-secret", f"A=1 PASSWORD={REDACTED}"),
])
def test_secret_after_a_non_secret_assignment_is_still_redacted(text, expected):
    assert redact(text) == expected
    for fragment in ("x,y", "sk,live", "t1,t2", "p1;p2", "remaining-secret"):
        assert fragment not in redact(text)


# ── second-review finding (T18, gate 7): a value that LOOKS sanitized is not ──────
# The already-sanitized whitelist exists so `Authorization: Bearer [REDACTED]` is not
# re-redacted into uselessness. Without requiring the marker itself, it accepted real
# credentials that happened to be a scheme word or punctuation. These are passwords whose
# value is literally "basic" or "-----", absent from the configured literal snapshot.
@pytest.mark.parametrize("text", [
    "PASSWORD=basic", "PASSWORD=bearer", "PASSWORD=token", "PASSWORD=key",
    "PASSWORD=digest", "TOKEN=basic", "API_KEY=key",
    "PASSWORD=-----", "PASSWORD=--------------------", 'PASSWORD=":;,."',
    'PASSWORD="key token"', "PASSWORD=basic basic", "PASSWORD='key'",
])
def test_scheme_words_and_punctuation_are_not_proof_of_redaction(text):
    out = redact(text)
    assert out != text, f"passed through unredacted: {out!r}"
    assert REDACTED in out
    key = text.split("=", 1)[0]
    assert out == f"{key}={REDACTED}", out


@pytest.mark.parametrize(("text", "expected"), [
    # ...while genuinely sanitized output keeps its scheme word and is left alone.
    ("Authorization: Bearer [REDACTED]", "Authorization: Bearer [REDACTED]"),
    ("authorization: bEaReR [REDACTED]", "authorization: bEaReR [REDACTED]"),
    ("PASSWORD=[REDACTED]", "PASSWORD=[REDACTED]"),
    ("PASSWORD: [REDACTED]", "PASSWORD: [REDACTED]"),
    ('{"PASSWORD":[REDACTED]', '{"PASSWORD":[REDACTED]'),
    ("PASSWORD=[REDACTED]\nB=2", "PASSWORD=[REDACTED]\nB=2"),
])
def test_already_redacted_output_survives_another_pass(text, expected):
    assert redact(text) == expected


# ── second-review finding (T18, gate 8): a marker for ANOTHER value on the line ──
# The exemption used to accept "a marker appears somewhere in the remainder". The Bearer
# pass runs first and supplies a marker for a different value on the same line, so a real
# password that happened to be a scheme word or punctuation was preserved beside it.
# The exemption is now an exact shape list, and the scheme shape is gated on an auth key.
@pytest.mark.parametrize("text", [
    "PASSWORD=basic Bearer abc.def",          # gate 8, verbatim
    "PASSWORD=----- Bearer abc.def",          # gate 8, verbatim
    "PASSWORD=basic [REDACTED]",              # scheme shape under a non-auth key
    "PASSWORD=---- [REDACTED]",
    "PASSWORD=basic TOKEN=[REDACTED]",
    "PASSWORD=key Authorization: Bearer abc.def",
    "PASSWORD=token bearer [REDACTED]",
    "TOKEN=basic Bearer abc.def",
    "API_KEY=key Bearer abc.def",
    "PASSWORD=basic https://u:p@h/x",
])
def test_a_marker_elsewhere_on_the_line_does_not_exempt_the_value(text):
    out = redact(text)
    key = re.split(r"[:=]", text, maxsplit=1)[0]
    assert out == f"{key}={REDACTED}" or out == f"{key}: {REDACTED}", out
    for fragment in ("basic", "-----", "----", "token", "key", "abc.def", "u:p@h"):
        assert fragment not in out.replace(REDACTED, ""), f"{fragment!r} survived in {out!r}"


# ── second-review finding (T18, gate 9): the exemption must match EXACTLY ─────────
# Both comparisons in the exemption used to be fuzzy, and each fuzziness leaked:
# punctuation-stripping made a password that merely contains "REDACTED" compare equal to
# the marker, and a substring test on "auth" let a password assignment claim the
# Authorization-header exemption.
@pytest.mark.parametrize(("text", "leaked_fragment"), [
    ("PASSWORD=---REDACTED---", "---REDACTED---"),      # gate 9, verbatim
    ("PASSWORD=REDACTED", "REDACTED"),
    ("PASSWORD=.REDACTED.", ".REDACTED."),
    ("PASSWORD=<REDACTED>", "<REDACTED>"),
    ("TOKEN=--REDACTED--", "--REDACTED--"),
    ("AUTH_PASSWORD=basic [REDACTED]", "basic"),        # gate 9, verbatim
    ("AUTH_TOKEN=basic [REDACTED]", "basic"),
    ("OAUTH_SECRET=bearer [REDACTED]", "bearer"),
])
def test_marker_lookalikes_and_auth_named_keys_are_not_exempt(text, leaked_fragment):
    out = redact(text)
    assert out != text, f"passed through unredacted: {out!r}"
    key = text.split("=", 1)[0]
    assert out == f"{key}={REDACTED}", out
    # The marker itself contains "REDACTED", so compare against the output with the
    # marker removed -- otherwise the assertion passes on the marker, not on the secret.
    assert leaked_fragment not in out.replace(REDACTED, ""), out


# ── second-review finding (T18, gate 10): camelCase keys ─────────────────────────
@pytest.mark.parametrize("text", [
    '{"accessToken":"unknown-secret"}',      # gate 10, verbatim
    '{"dbPassword":"unknown-secret"}',       # gate 10, verbatim
    "clientSecret=unknown-secret",
    "refreshToken: unknown-secret",
    '{"authKey":"unknown-secret"}',
])
def test_camel_case_sensitive_keys_are_redacted(text):
    out = redact(text)
    assert "unknown-secret" not in out, out
    assert REDACTED in out


@pytest.mark.parametrize("text", [
    '{"keyboardLayout":"us"}', '{"passengerCount":4}', '{"authorName":"jane"}',
    '{"tokenizerModel":"bpe"}', "monkeyCount=3", "passageId=7",
])
def test_camel_case_innocent_keys_are_untouched(text):
    assert redact(text) == text


@pytest.mark.parametrize("text", [
    # Real authorization headers keep their scheme word. These are the only two shapes
    # redact() emits, and the exemption exists solely to preserve them.
    "Authorization: Bearer [REDACTED]",
    "authorization: bEaReR [REDACTED]",
    "AUTHORIZATION: Basic [REDACTED]",
    "Proxy_Authorization: Bearer [REDACTED]",
])
def test_authorization_headers_keep_their_scheme_word(text):
    assert redact(text) == text


# ── second-review finding (T18, gates 6 and 7): no super-linear path ─────────────
# Three separate quadratic paths shipped and were caught in review, each an unbounded
# regex repetition: the key scan, the whitespace run, and the URL scheme class. The
# bounds are load-bearing, so a generous ceiling here fails loudly if one is removed.
# Ceilings are ~50x the measured times (0.8s worst case) to stay non-flaky on slow CI.
# The inputs are built lazily behind `ids`: parametrizing on the strings themselves puts
# 200k characters into the test id, which made one pytest run emit 1.3MB of output.
@pytest.mark.parametrize("build", [
    lambda: "a." * 100000,                             # was quadratic: URL scheme class
    lambda: "a." * 100000 + "://x@y",
    lambda: "x" * 200000,                              # was quadratic: key scan
    lambda: "x" + ":" * 200000,
    lambda: "message: x" + " " * 200000 + "ok",        # was quadratic: [ \t]*
    lambda: "A=" * 100000 + "x",
    lambda: "PASSWORD=s3cret " * 12000,
], ids=["dot run", "scheme run", "identifier run", "separator run",
        "whitespace run", "assignment run", "secret heavy"])
def test_no_superlinear_path(build):
    import time

    text = build()
    start = time.perf_counter()
    redact(text)
    elapsed = time.perf_counter() - start
    assert elapsed < 5.0, f"{elapsed:.2f}s -- an unbounded regex repetition is back"
