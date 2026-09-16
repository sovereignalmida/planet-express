"""
planet_express/core/redact.py — secret removal at core output boundaries.

Applied wherever command output leaves core: the planner's evidence records, Amy's
diagnosis prompt, and the on-disk step log. Configured credentials are snapshotted once at
import; redact() has no I/O and no mutable state.

DESIGN: withhold to end of line. Do NOT try to find where a sensitive value ENDS.

Six review rounds of a value-parsing scanner leaked seven different ways, every one of
them an answer to "where does this value stop?" — prefix truncation on punctuation, a
greedy value swallowing a later secret, JSON and bare-colon forms passing through, an
escaped quote ending a value early, a Bearer prefix bypassing full-value redaction, and a
marker-exemption that let `PASSWORD=[REDACTED],TOKEN=real-secret` through untouched. It
also produced three super-linear paths and a RecursionError. Parsing arbitrary command
output is a parser; this is a safety filter, and the two want opposite things.

So the value boundary is no longer computed at all:

    line ──► first sensitive KEY<sep> on the line (one forward scan)
               │
               ├─ remainder is exactly one of the two shapes redact() emits
               │  (`[REDACTED]`, or `<scheme> [REDACTED]` after an auth header)
               │  ──► leave the line alone
               └─ otherwise ──► KEY<sep>[REDACTED] and DROP THE REST OF THE LINE

Dropping the rest of the line loses innocent content after a secret: `A=1 PASSWORD=x B=2`
keeps `A=1 PASSWORD=[REDACTED]` and loses `B=2`. That is the accepted cost. A mangled log
line costs a reader some context; a leaked token costs a credential.

Properties this buys, none of which the parsing design ever held at the same time:
  - No quote, escape, or nesting logic exists, so none of it can be wrong.
  - Idempotent by construction: the output's own remainder is one of the emitted shapes, so a second
    pass leaves it alone.
  - Linear: one `finditer` per line, no backward walking, no recursion.

Per line, the key cut, configured literal secrets (overlaps included), `Bearer <token>` and
URL userinfo are ALL located on the untouched input, then masked once as a union. Every fixed
sequential order of those passes leaked in review (see _redact_line).
"""

import bisect
import re

import config

REDACTED = "[REDACTED]"
MAX_REDACT_CHARS = 256 * 1024


def _read_secrets() -> tuple[str, ...]:
    values = set()
    for accessor in (
        config.anthropic_api_key, config.openai_api_key,
        config.telegram_credentials, config.adguard_credentials,
    ):
        try:
            credentials = accessor()
        except Exception:  # noqa: BLE001, S112 -- optional accessors must never break redaction
            continue
        if isinstance(credentials, str):
            credentials = (credentials,)
        values.update(value for value in credentials if isinstance(value, str) and value)
    # Longest first so an overlapping shorter secret cannot leave a tail behind.
    return tuple(sorted(values, key=lambda value: (-len(value), value)))


_SECRETS = _read_secrets()
# REDACTED itself is in the alternation so a second pass cannot damage the marker.
_LITERAL_RE = re.compile("|".join(re.escape(v) for v in (REDACTED, *_SECRETS)))

# Segment rule: delimited by start/end or underscore, so DB_PASSWORD matches while
# KEYBOARD=us, PASSAGE=3 and MONKEY=1 survive untouched.
_SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:pass|password|passwd|secret|token|key|auth|authorization)(?:_|$)",
    re.IGNORECASE,
)
# Run-together spellings the segment rule misses (measured: APIKEY=, apikey=, SECRETKEY=,
# AUTHTOKEN= all leaked). A list, not a pattern: no lexical rule separates MYKEY from
# MONKEY, so MYKEY= stays an accepted gap covered by the literal snapshot instead.
_SECRET_KEY_COMPOUNDS = (
    "apikey", "apitoken", "secretkey", "authtoken", "accesskey", "privatekey",
    "bearertoken", "sessionkey", "signingkey",
)

# One forward scan per line. Quotes and brackets are NOT in the identifier class, so a
# nested key is found on its own (MESSAGE="PASSWORD=x" yields MESSAGE, then PASSWORD) and
# a run of separators with no identifier (':' * 4000) matches nothing -- that run is what
# made an earlier backward-walking version quadratic.
#
# BOTH REPETITIONS ARE BOUNDED, and that is load-bearing, not tidiness. Unbounded
# `[A-Za-z0-9_]+` is quadratic on a long identifier run: on 200k non-separator characters
# the engine matches the whole run at every start position, fails to find a separator, and
# gives back one character at a time (measured: no result in 120s). Unbounded `[ \t]*` does
# the same on a long whitespace run. Bounding both caps the backtrack per position, so the
# scan is linear in line length.
#
# Cost of the bound: a sensitive key longer than 128 characters, or separated from its
# value by more than 32 spaces, is not recognised. Real env keys and log formats are far
# inside both; the configured-literal pass is the backstop.
#
# The `["']{0,2}` between key and separator is what covers JSON. A quoted key does not
# touch its separator ({"PASSWORD":"s3cret"} has a quote in between), so a scanner that
# requires adjacency misses every JSON object -- measured: both JSON shapes passed through
# completely unredacted. An earlier design reached the key by walking backward over
# punctuation instead, which is precisely the quadratic path review flagged. Bounded and
# forward-only gets JSON without reintroducing it.
_ASSIGNMENT_RE = re.compile(r"([A-Za-z0-9_]{1,128})[\"']{0,2}[ \t]{0,32}[:=][ \t]{0,32}")

_BEARER_RE = re.compile(r"\b(Bearer[ \t]+)[^\s\"',;]+", re.IGNORECASE)
# Scheme length bounded for the same reason as _ASSIGNMENT_RE: unbounded `[A-Za-z0-9+.-]*`
# is quadratic on input like "a.a.a.a..." -- every letter is a word boundary, so the class
# consumes the rest of the string and fails to find "://" at each one (measured: 0.085s at
# 16k, 0.336s at 32k, still inside MAX_REDACT_CHARS). No real URL scheme is near 32 chars.
_USERINFO_RE = re.compile(r"\b([A-Za-z][A-Za-z0-9+.-]{0,32}://)[^\s/@]{1,512}@")
# Scheme words are safe to keep: they say HOW something authenticated, not with what.
_AUTH_SCHEMES = frozenset({"bearer", "basic", "digest", "token", "apikey", "key"})


def _is_secret_key(key: str) -> bool:
    if not key:
        return False
    # Segment check on the camelCase-split key, not just the raw one: `accessToken` and
    # `dbPassword` have no underscore, so the segment rule never saw `token`/`password` as
    # a segment, and flattening removed the split again before the compound check (review
    # gate 10: both JSON fields leaked). All-caps keys have no camel boundary, so MONKEY
    # and KEYBOARD are unaffected; keyboardLayout splits to keyboard_Layout, still no match.
    split = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    if _SECRET_KEY_RE.search(key) or _SECRET_KEY_RE.search(split):
        return True
    flat = split.lower().replace("_", "")
    return any(compound in flat for compound in _SECRET_KEY_COMPOUNDS)


# The marker's exact syntax, brackets included, optionally wrapped in the quotes and
# trailing structure redact() itself leaves around it ('{"K":"[REDACTED]"}', '[REDACTED],').
# ANCHORED AND EXACT: an earlier version compared token.strip(_PUNCTUATION) against
# "REDACTED", which made the real password `---REDACTED---` compare equal to the marker and
# pass through untouched.
# The leading set deliberately excludes '[': the marker supplies its own bracket, so
# allowing another one matched `[[REDACTED]]` -- not a marker, therefore a real value.
_MARKER_TOKEN_RE = re.compile(r"^[\"'{(]*\[REDACTED\][\"')}\],;]*$")
# Scheme preservation belongs to authorization HEADERS, enumerated. An earlier version
# tested `"auth" in key.lower()`, which `AUTH_PASSWORD=basic [REDACTED]` satisfies -- that
# is a password assignment, and `basic` was its value.
_AUTH_HEADER_KEYS = frozenset({"authorization", "proxy_authorization", "www_authenticate"})


def _is_sanitized_value(remainder: str, key: str) -> bool:
    """True ONLY for the two shapes redact() itself emits after a key:

        [REDACTED]              -- the normal case, after any sensitive key
        <scheme> [REDACTED]     -- `Authorization: Bearer [REDACTED]`, AUTH HEADERS ONLY

    This exists so a second pass does not chew already-safe output into uselessness. It is
    an EXACT shape list rather than a rule about tokens, because four earlier versions
    asked a looser question and every one of them leaked a real credential:

      1. "is every token harmless?" -- accepted `PASSWORD=basic` and `PASSWORD=-----`,
         passwords that merely happen to be a scheme word or punctuation.
      2. "...and does a marker appear anywhere?" -- still accepted
         `PASSWORD=basic Bearer [REDACTED]`, because the earlier Bearer pass supplies a
         marker for a DIFFERENT value on the same line, exempting the real password.
      3. "...with punctuation stripped before comparing" -- accepted `---REDACTED---`,
         a password that is not the marker at all.
      4. "...for any key containing auth" -- accepted `AUTH_PASSWORD=basic [REDACTED]`.

    Every one of those was a FUZZY match guarding a security boundary. Both comparisons
    here are exact: the marker is matched by its real syntax, the key against an enumerated
    set. A loose description of safe output can be satisfied by combining harmless-looking
    parts; an enumeration of what we actually emit cannot.
    """
    tokens = remainder.split()
    if len(tokens) == 1:
        return bool(_MARKER_TOKEN_RE.match(tokens[0]))
    return (
        len(tokens) == 2
        and key.lower() in _AUTH_HEADER_KEYS
        and tokens[0].strip("\"'").lower() in _AUTH_SCHEMES
        and bool(_MARKER_TOKEN_RE.match(tokens[1]))
    )


def _marker_spans(text: str) -> list[tuple[int, int]]:
    spans, pos = [], text.find(REDACTED)
    while pos != -1:
        spans.append((pos, pos + len(REDACTED)))
        pos = text.find(REDACTED, pos + len(REDACTED))
    return spans


def _literal_spans(text: str) -> list[tuple[int, int]]:
    """Every configured-secret occurrence, INCLUDING overlapping ones.

    finditer returns non-overlapping matches only: with configured `admin` and
    `min-secret`, `admin-secret` matched `admin` and left `-secret` exposed (gate 13).
    Searching again from each match start + 1 finds every start position that has a
    match, and _mask() redacts their union. Matches wholly inside an existing marker are
    skipped, so a configured value like `RED` cannot chew the marker on a second pass.
    """
    markers = _marker_spans(text)
    marker_starts = [start for start, _ in markers]
    spans, match = [], _LITERAL_RE.search(text)
    while match:
        start, end = match.span()
        if match[0] != REDACTED and end > start:
            i = bisect.bisect_right(marker_starts, start) - 1
            if not (i >= 0 and end <= markers[i][1]):
                spans.append((start, end))
        match = _LITERAL_RE.search(text, start + 1)
    return spans


def _secret_spans(text: str, literals: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Every span to withhold, located on the ORIGINAL text before anything changes.

    Bearer and userinfo spans are found here rather than by rewriting the text first: a
    configured value equal to `Bearer` destroyed the syntax the Bearer pass needs, and the
    whole token leaked (gate 13).
    """
    spans = list(literals)
    spans += [(m.end(1), m.end()) for m in _BEARER_RE.finditer(text)]
    spans += [(m.end(1), m.end() - 1) for m in _USERINFO_RE.finditer(text)]
    return spans


def _mask(text: str, spans: list[tuple[int, int]]) -> str:
    """Replace the UNION of spans with one marker per merged run."""
    merged: list[list[int]] = []
    for start, end in sorted((s, e) for s, e in spans if e > s):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, pos = [], 0
    for start, end in merged:
        out.append(text[pos:start])
        out.append(REDACTED)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _redact_line(line: str) -> str:
    """Every detection runs against the ORIGINAL line; replacement happens once, at the end.

    Each sequential ordering of the passes leaked, proven from several sides in review:
      - literals first hid a key name (ADGUARD_PASSWORD=token hid `access_token=x`, gate 11);
      - the Bearer pass first split a configured literal (`Bearer abc,rest`, gate 12);
      - literals first destroyed Bearer syntax (ADGUARD_PASSWORD=Bearer, gate 13).
    So nothing is rewritten until all spans and the key cut are known.
    """
    literals = _literal_spans(line)
    spans = _secret_spans(line, literals)
    for match in _ASSIGNMENT_RE.finditer(line):
        if not _is_secret_key(match[1]):
            continue                      # keep scanning: a nested key may follow
        cut = match.end()
        prefix = _mask(line[:cut], [(s, min(e, cut)) for s, e in spans if s < cut])
        remainder = _mask(line[cut:], [(max(s, cut) - cut, e - cut) for s, e in spans if e > cut])
        literal_after_cut = any(e > cut for _, e in literals)
        if not literal_after_cut and _is_sanitized_value(remainder, match[1]):
            return prefix + remainder
        # Everything after the key goes, whatever shape it has. No attempt to find its end.
        return prefix + REDACTED
    return _mask(line, spans)


def redact(text: str) -> str:
    """Remove known credentials, sensitive assignments, bearer tokens and userinfo."""
    if not text:
        return text
    if len(text) > MAX_REDACT_CHARS:
        return REDACTED
    # A configured value spanning a newline can't be seen per line; mask those first. This
    # can only ever hide text, and a newline-bearing secret is not a key name.
    if "\n" in text:
        text = _mask(text, [(s, e) for s, e in _literal_spans(text) if "\n" in text[s:e]])
    return "\n".join(_redact_line(line) for line in text.split("\n"))
