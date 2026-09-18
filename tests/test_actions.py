"""
planet_express/execution/actions.py (landing 1a, T5). Zoidberg's original semantics are
pinned in tests/test_zoidberg_health_regression.py; this file covers what the extraction
added: the restart-count baseline the 1b verifier will use, the compose-label lookup
Amy's investigation now shares, and that nothing here ever builds a shell string.
"""

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("CASA_CONFIG", str(Path(__file__).resolve().parent.parent / "config.example.yaml"))

from planet_express.execution import actions


class FakeRunArgv:
    def __init__(self, *responses):
        self.responses = list(responses)  # [(rc, stdout, stderr), ...]
        self.calls: list[list[str]] = []

    def __call__(self, argv, timeout):
        assert isinstance(argv, list)
        assert timeout == actions.DOCKER_TIMEOUT_SECONDS
        self.calls.append(argv)
        return self.responses.pop(0)


def _install(monkeypatch, fake):
    monkeypatch.setattr(actions.bender, "run_argv", fake)
    monkeypatch.setattr("time.sleep", lambda _s: None)
    return fake


# ── baseline_restarts (used by the 1b typed-action verifier) ────────────────────
def test_restarts_before_the_action_do_not_fail_verification(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "running\t5\thealthy", "")))
    assert actions.container_health("fixture-crash-loop", baseline_restarts=5) == (True, "ok")


def test_restart_after_the_baseline_fails_with_the_delta(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "running\t6\thealthy", "")))
    assert actions.container_health("fixture-crash-loop", baseline_restarts=5) == (
        False, "restarted 1x during watch window",
    )


def test_watch_until_stable_passes_the_baseline_to_every_poll(monkeypatch):
    fake = _install(monkeypatch, FakeRunArgv((0, "running\t3\thealthy", ""), (0, "running\t3\thealthy", "")))
    assert actions.watch_until_stable("web", seconds=10, poll_seconds=5, baseline_restarts=3) == (True, "ok")
    assert len(fake.calls) == 2


# ── container_compose_labels (Amy's investigation) ──────────────────────────────
def test_compose_labels_parsed(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "media\tsonarr\n", "")))
    assert actions.container_compose_labels("CASA_SONARR") == ("media", "sonarr")


def test_compose_labels_fall_back_for_non_compose_container(monkeypatch):
    _install(monkeypatch, FakeRunArgv((0, "\t", "")))
    assert actions.container_compose_labels("CASA_STANDALONE") == ("unknown", "CASA_STANDALONE")


def test_compose_labels_fall_back_when_inspect_fails(monkeypatch):
    _install(monkeypatch, FakeRunArgv((1, "", "Error: No such object: ghost")))
    assert actions.container_compose_labels("ghost") == ("unknown", "ghost")


def test_hostile_container_name_stays_one_argv_element(monkeypatch):
    hostile = "CASA_X; docker rm -f $(docker ps -aq)"
    fake = _install(monkeypatch, FakeRunArgv((1, "", "no such object")))
    actions.container_compose_labels(hostile)
    assert fake.calls[0][-1] == hostile
    assert fake.calls[0][:3] == ["docker", "inspect", "--format"]


def test_strict_compose_identities_batch_and_omit_incomplete_labels(monkeypatch):
    fake = _install(monkeypatch, FakeRunArgv((
        0, "/CASA_WEB\tmedia\tweb\n/CASA_BARE\tmedia\t\n", "",
    )))
    assert actions.container_compose_identities(["CASA_WEB", "CASA_BARE"]) == {
        "CASA_WEB": ("media", "web")
    }
    assert fake.calls[0][-2:] == ["CASA_WEB", "CASA_BARE"]


def test_strict_compose_identities_accept_long_valid_container_name(monkeypatch):
    name = "project_" + "service" * 20
    _install(monkeypatch, FakeRunArgv((0, f"/{name}\tmedia\tweb\n", "")))
    assert actions.container_compose_identities([name]) == {name: ("media", "web")}


def test_strict_compose_identities_fail_closed(monkeypatch):
    _install(monkeypatch, FakeRunArgv((actions.bender.RUN_ARGV_TIMEOUT_EXIT, "", "")))
    with pytest.raises(actions.TargetTimeout):
        actions.container_compose_identities(["CASA_WEB"])
    with pytest.raises(actions.TargetError):
        actions.container_compose_identities(["bad/name"])


def test_service_container_passes_stack_path_with_spaces_intact(monkeypatch):
    fake = _install(monkeypatch, FakeRunArgv((0, "abc123", ""), (0, "/my-app", "")))
    assert actions.service_container(Path("/home/casaroot/stacks/my stack"), "web") == "my-app"
    assert fake.calls[0] == [
        "docker", "compose", "-f", "/home/casaroot/stacks/my stack/docker-compose.yml", "ps", "-q", "web",
    ]


def test_restart_capabilities():
    assert actions.REGISTRY[actions.RESTART_SERVICE].capabilities() == {
        'abortable': False, 'rollbackable': False, 'resumable': False,
    }


def test_stats_argv_uses_fixed_template():
    assert actions.stats_argv('container') == [
        'docker', 'stats', '--no-stream', '--format',
        '{{.CPUPerc}}\t{{.MemUsage}}\t{{.MemPerc}}', 'container',
    ]


def test_stats_unit_conversions():
    units = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3,
             'kB': 1000, 'MB': 1000**2, 'GB': 1000**3}
    for unit, multiplier in units.items():
        assert actions.parse_stats(f'125.5%\t1.5{unit} / 2 {unit}\t75.00%') == {
            'cpu_percent': 125.5, 'memory_percent': 75.0,
            'memory_used_bytes': int(1.5 * multiplier), 'memory_limit_bytes': 2 * multiplier,
        }


def test_stats_malformed_output():
    for value in (None, 42, b'bad', '', 'bad', '1%\t2MB\t3%', '1%\t2XB / 3GB\t3%',
                  'NaN%\t1B / 2B\t3%', 'inf%\t1B / 2B\t3%', '-1%\t1B / 2B\t3%',
                  '1\t1B / 2B\t3%', '1%\t1B / 2B\t3%\nextra',
                  '1%\t' + '9' * 400 + 'GB / 2B\t3%'):
        assert actions.parse_stats(value) is None


def test_read_stats_typed_errors_and_success(monkeypatch):
    for response, expected in (
        ((actions.bender.RUN_ARGV_TIMEOUT_EXIT, '', 'secret'), {'ok': False, 'error': 'timeout'}),
        ((1, '', 'secret'), {'ok': False, 'error': 'unavailable'}),
        ((0, 'bad', 'secret'), {'ok': False, 'error': 'unavailable'}),
        ((0, '1%\t1MiB / 2GiB\t3%', ''),
         {'ok': True, 'stats': actions.parse_stats('1%\t1MiB / 2GiB\t3%')}),
    ):
        def run(argv, timeout, response=response):
            assert argv == actions.stats_argv('c') and timeout == 4
            return response
        monkeypatch.setattr(actions.bender, 'run_argv', run)
        assert actions.read_stats('c', timeout=4) == expected


def test_parse_stats_accepts_terabyte_units():
    from planet_express.execution import actions

    assert actions.parse_stats("0.5%\t1.5GiB / 1.25TiB\t0.1%")["memory_limit_bytes"] == int(1.25 * 1024**4)
    assert actions.parse_stats("0.5%\t3MB / 2TB\t0.1%")["memory_limit_bytes"] == 2 * 1000**4
    assert actions.parse_stats("0.5%\t3MB / 2EB\t0.1%") is None


TS = '2026-09-17T12:00:00.123456789Z'


def _logs(monkeypatch, stdout, stderr='', **kwargs):
    calls = []

    def run(argv, timeout):
        calls.append((argv, timeout))
        return 0, TS, ''

    def bounded(argv, timeout, max_bytes):
        assert argv[1] == 'logs' and max_bytes == actions.LOG_CAPTURE_MAX_BYTES
        calls.append((argv, timeout))
        return 0, stdout, stderr, kwargs.get('truncated', False)

    monkeypatch.setattr(actions.bender, 'run_argv', run)
    monkeypatch.setattr(actions.bender, 'run_argv_bounded', bounded)
    result = actions.read_logs('c', cursor=kwargs.get('cursor'),
                               cursor_hashes=kwargs.get('cursor_hashes', []), timeout=4)
    assert all(0 < timeout <= 4 for _, timeout in calls)
    return result


def test_logs_argv():
    assert actions.logs_argv('c', None) == ['docker', 'logs', '--timestamps', '--tail', '500', 'c']
    assert actions.logs_argv('c', TS) == ['docker', 'logs', '--timestamps', '--tail', '500', '--since', TS, 'c']


def test_logs_merge_multiline_sort_and_cursor_dedupe(monkeypatch):
    earlier = '2026-09-17T12:00:00Z'
    out = f'{TS} out\ncontinuation\n{earlier} early'
    err = f'{TS} err'
    result = _logs(monkeypatch, out, err)
    assert [r['text'] for r in result['lines']] == ['early', 'out', 'continuation', 'err']
    assert [r['stream'] for r in result['lines']] == ['stdout', 'stdout', 'stdout', 'stderr']
    assert result['cursor'] == result['started_at'] == TS
    assert not result['skipped']
    again = _logs(monkeypatch, out, err, cursor=TS, cursor_hashes=result['cursor_hashes'])
    assert [r['text'] for r in again['lines']] == ['early']
    empty = _logs(monkeypatch, f'{TS} err', cursor=TS, cursor_hashes=result['cursor_hashes'])
    assert empty['lines'] == [] and empty['cursor'] == TS


def test_logs_caps_keep_newest(monkeypatch):
    result = _logs(monkeypatch, '\n'.join(f'{TS} line {i}' for i in range(510)))
    assert len(result['lines']) == 500 and result['skipped']
    assert result['lines'][0]['text'] == 'line 10'
    result = _logs(monkeypatch, '\n'.join(f'{TS} {i:03d} ' + 'é' * 2046 for i in range(100)))
    assert result['skipped'] and 0 < len(result['lines']) < 100
    serialized = sum(len(json.dumps(r).encode()) + 1 for r in result['lines'])
    assert serialized <= actions.LOG_RESPONSE_BUDGET_BYTES
    assert result['lines'][-1]['text'].startswith('099 ')          # newest kept


def test_logs_budget_counts_json_escapes_so_the_response_fits_the_transport(monkeypatch):
    # ESC and other control characters are 1 byte of text but 6 bytes of JSON.
    esc = '\x1b' * 3000
    result = _logs(monkeypatch, '\n'.join(f'{TS} {i:03d}' + esc for i in range(500)))
    assert result['skipped']
    payload = json.dumps({"ok": True, "result": result}).encode()
    assert len(payload) < 1024 * 1024
    assert sum(len(json.dumps(r).encode()) + 1 for r in result['lines']) <= actions.LOG_RESPONSE_BUDGET_BYTES


def test_logs_all_duplicate_poll_keeps_the_seen_hashes(monkeypatch):
    first = _logs(monkeypatch, f'{TS} a\n{TS} b')
    assert len(first['cursor_hashes']) == 2
    again = _logs(monkeypatch, f'{TS} a\n{TS} b', cursor=TS, cursor_hashes=first['cursor_hashes'])
    assert again['lines'] == [] and again['cursor'] == TS
    assert set(again['cursor_hashes']) == set(first['cursor_hashes'])
    third = _logs(monkeypatch, f'{TS} a\n{TS} b\n{TS} c', cursor=TS, cursor_hashes=again['cursor_hashes'])
    assert [r['text'] for r in third['lines']] == ['c']              # a and b never replay
    assert len(third['cursor_hashes']) == 3


def test_logs_returned_hashes_are_always_a_valid_next_request(monkeypatch):
    out = '\n'.join(f'{TS} line {i}' for i in range(500))
    first = _logs(monkeypatch, out)
    assert len(first['cursor_hashes']) == 500
    state = first
    for _ in range(3):
        state = _logs(monkeypatch, out + f'\n{TS} more', cursor=TS, cursor_hashes=state['cursor_hashes'])
        assert len(state['cursor_hashes']) <= actions.LOG_CURSOR_HASH_LIMIT


def test_logs_redact_before_byte_truncation(monkeypatch):
    import importlib
    import re

    redaction = importlib.import_module('planet_express.core.redact')
    secret = 'boundary-secret'
    monkeypatch.setattr(redaction, '_LITERAL_RE', re.compile(r'\[REDACTED\]|' + secret))
    result = _logs(monkeypatch, f'{TS} API_KEY=planted\n{TS} ' + 'x' * 4090 + secret + 'z' * 20)
    assert result['lines'][0]['text'] == 'API_KEY=[REDACTED]'
    text = result['lines'][1]['text']
    assert 'boundary' not in text and text.endswith('…')
    assert len(text.encode()) <= 4096
    # Shortening one line is not a gap: the ellipsis marks it, `skipped` stays False.
    assert not result['skipped']


def test_logs_full_docker_tail_is_flagged_as_a_possible_gap(monkeypatch):
    # docker's own --tail 500 already dropped anything older, so 500 returned lines may hide a gap.
    exactly = _logs(monkeypatch, '\n'.join(f'{TS} line {i}' for i in range(300)),
                    '\n'.join(f'{TS} err {i}' for i in range(200)))
    assert len(exactly['lines']) == 500 and exactly['skipped']
    under = _logs(monkeypatch, '\n'.join(f'{TS} line {i}' for i in range(499)))
    assert len(under['lines']) == 499 and not under['skipped']


def test_logs_failures_and_exhausted_started_at(monkeypatch):
    for rc, error in [(124, 'timeout'), (1, 'unavailable')]:
        monkeypatch.setattr(actions.bender, 'run_argv_bounded', lambda *a, rc=rc, **k: (rc, '', 'SECRET', False))
        assert actions.read_logs('c', cursor=None, cursor_hashes=[], timeout=4) == {'ok': False, 'error': error}
    from types import SimpleNamespace
    now = [0]
    monkeypatch.setattr(actions, 'time', SimpleNamespace(monotonic=lambda: now[0]))

    def bounded(argv, timeout, max_bytes):
        assert argv[1] == 'logs'
        now[0] = 4
        return 0, f'{TS} hello', '', False

    monkeypatch.setattr(actions.bender, 'run_argv_bounded', bounded)
    def no_inspect(*a, **k):
        raise AssertionError('started_at inspect must be skipped once the budget is exhausted')

    monkeypatch.setattr(actions.bender, 'run_argv', no_inspect)
    assert actions.read_logs('c', cursor=None, cursor_hashes=[], timeout=4)['started_at'] is None


def test_facts_allowlist_and_parsing(monkeypatch):
    import json

    for ports in (None, {}, {'80/tcp': None}, {'80/tcp': [
        {'HostIp': '0.0.0.0', 'HostPort': '8080'}, {'HostIp': '::', 'HostPort': '8080'}]}):
        for health, streak in [('none', 0), ('unhealthy', 7)]:
            def run(argv, timeout, health=health, streak=streak, ports=ports):
                assert argv == ['docker', 'inspect', '--format', actions.FACTS_FORMAT, 'c']
                assert '.Config' not in argv[3] and timeout == 4
                return 0, f'running\t{TS}\t{health}\t{streak}\t3\ton-failure\t5\t{json.dumps(ports)}\tsha256:abc', ''
            monkeypatch.setattr(actions.bender, 'run_argv', run)
            facts = actions.read_facts('c', timeout=4)['facts']
            assert facts == {'state': 'running', 'started_at': TS, 'health': health,
                             'failing_streak': streak, 'restart_count': 3,
                             'restart_policy': {'name': 'on-failure', 'max_retries': 5},
                             'image_id': 'sha256:abc', 'ports': [
                                 {'container_port': '80', 'protocol': 'tcp', 'host_ip': b['HostIp'],
                                  'host_port': b['HostPort']} for b in (ports or {}).get('80/tcp') or []]}
    for rc, out, error in [(124, '', 'timeout'), (1, '', 'unavailable'), (0, 'bad', 'unavailable')]:
        monkeypatch.setattr(actions.bender, 'run_argv', lambda *a, rc=rc, out=out, **k: (rc, out, 'SECRET'))
        assert actions.read_facts('c', timeout=4) == {'ok': False, 'error': error}


def test_logs_budget_uses_the_transports_ascii_escaping(monkeypatch):
    # 100 lines of 2000 "é": ~200 KiB as UTF-8 but ~1.2 MB as the transport's escaped JSON.
    result = _logs(monkeypatch, '\n'.join(f'{TS} {i:03d} ' + 'é' * 2000 for i in range(100)))
    assert result['skipped']
    assert len(json.dumps(result['lines']).encode()) <= actions.LOG_RESPONSE_BUDGET_BYTES + 2
    assert len(json.dumps({"ok": True, "result": result}).encode()) < 1024 * 1024


def test_logs_blank_final_entry_is_an_empty_line_not_a_continuation(monkeypatch):
    later = '2026-09-17T12:30:00.5Z'
    result = _logs(monkeypatch, f'{TS} hello\n{later}')
    assert [(r['ts'], r['text']) for r in result['lines']] == [(TS, 'hello'), (later, '')]
    assert result['cursor'] == later


def test_logs_capture_truncation_is_reported_as_skipped(monkeypatch):
    result = _logs(monkeypatch, f'{TS} newest', truncated=True)
    assert result['skipped'] and [r['text'] for r in result['lines']] == ['newest']


def test_logs_trailing_whitespace_does_not_change_the_hash_between_polls(monkeypatch):
    # First poll: the record is last, so the raw output ends in its trailing spaces.
    first = _logs(monkeypatch, f'{TS} padded   ')
    # Next poll: the same record is no longer last.
    later = '2026-09-17T12:30:00Z'
    again = _logs(monkeypatch, f'{TS} padded   \n{later} new', cursor=TS, cursor_hashes=first['cursor_hashes'])
    assert [r['text'] for r in again['lines']] == ['new']


def test_logs_split_only_on_newline_so_redaction_sees_whole_records(monkeypatch):
    # splitlines() also breaks on \v \f \r and U+2028/9; docker does not use those as record
    # separators, and splitting there let the tail of a secret through unredacted.
    for separator in ('\v', '\f', ' ', ' '):
        result = _logs(monkeypatch, f'{TS} PASSWORD=first{separator}second')
        texts = [r['text'] for r in result['lines']]
        assert texts == ['PASSWORD=[REDACTED]'], (separator, texts)
    crlf = _logs(monkeypatch, f'{TS} one\r\n{TS} two')
    assert [r['text'] for r in crlf['lines']] == ['one', 'two']


def test_logs_processing_respects_the_deadline_and_keeps_the_newest(monkeypatch):
    import time as real_time
    from types import SimpleNamespace

    clock = [0.0]

    def monotonic():
        clock[0] += 0.35        # each redact "costs" 0.35s of the 4s budget
        return clock[0]

    monkeypatch.setattr(actions, 'time', SimpleNamespace(monotonic=monotonic))
    started = real_time.perf_counter()
    result = _logs(monkeypatch, '\n'.join(f'{TS} line {i:03d}' for i in range(500)))
    assert real_time.perf_counter() - started < 5
    assert result['skipped'] and 0 < len(result['lines']) < 500
    assert result['lines'][-1]['text'] == 'line 499'      # newest kept, oldest dropped


def test_logs_record_cap_keeps_the_newest_and_marks_the_gap(monkeypatch):
    many = '\n'.join(f'{TS} line {i}' if i % 2 == 0 else 'continuation'
                     for i in range(actions.LOG_MAX_RECORDS + 200))
    result = _logs(monkeypatch, many)
    assert result['skipped']
    assert result['lines'][-1]['text'] == 'continuation'


def test_logs_terminal_newline_does_not_add_a_blank_record(monkeypatch):
    result = _logs(monkeypatch, f'{TS} hello\n')
    assert [(r['ts'], r['text']) for r in result['lines']] == [(TS, 'hello')]
    # A real blank record (bare timestamp) is still kept.
    later = '2026-09-17T12:30:00Z'
    blank = _logs(monkeypatch, f'{TS} hello\n{later}\n')
    assert [(r['ts'], r['text']) for r in blank['lines']] == [(TS, 'hello'), (later, '')]


def test_logs_record_buffer_is_bounded_during_parsing(monkeypatch):
    import tracemalloc

    flood = f'{TS} first' + '\n' * 400_000
    tracemalloc.start()
    result = _logs(monkeypatch, flood)
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert result['skipped']
    assert len(result['lines']) <= actions.LOG_MAX_LINES
    assert peak < 40 * 1024 * 1024, f'peak {peak / 1e6:.0f} MB'
