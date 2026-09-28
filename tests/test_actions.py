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





# ── Which container declares which Traefik router (T46.1) ───────────────────────

_ID_A, _ID_B, _ID_C = ("a" * 64, "b" * 64, "c" * 64)
_ID_Q_ALT = "9" * 64


def _row(name, cid, labels, mode="bridge", running=True, status=None):
    status = status or ("running" if running else "exited")
    return f"/{name}\t{cid}\t{status}\t{mode}\t{json.dumps(labels)}"


def _owners(rows, ids=None):
    if ids is None:
        ids = [row.split("\t")[1] for row in rows]
    got = actions.parse_router_owners("\n".join(rows).strip(), ids)
    got.pop("containers")
    # These cases are about router and service claims; the icon label has its own tests below.
    got.pop("icons")
    return got


def _compose(service, project, **extra):
    return {"com.docker.compose.service": service, "com.docker.compose.project": project, **extra}


def test_explicit_router_and_service_labels_name_their_container():
    got = _owners([_row("CASA_ACTUAL", _ID_A, _compose("actual_server", "money", **{
        "traefik.enable": "true",
        "traefik.http.routers.actual.rule": "Host(`actual.casalan.com`)",
        "traefik.http.routers.actual-lan.rule": "Host(`actual.casalan.com`)",
        "traefik.http.services.actual.loadbalancer.server.port": "5006"}))])
    assert got == {"routers": {"actual": "CASA_ACTUAL", "actual-lan": "CASA_ACTUAL"},
                   "services": {"actual": "CASA_ACTUAL"}}


def test_explicit_routers_with_no_service_label_use_the_default_service():
    got = _owners([_row("CASA_X", _ID_A, _compose("x", "p", **{
        "traefik.http.routers.named.rule": "Host(`x`)"}))])
    assert got == {"routers": {"named": "CASA_X"}, "services": {"x-p": "CASA_X"}}


def test_no_labels_means_traefiks_default_router_and_service():
    """Traefik names both `<service>_<project>`, normalised: compose service `wiki-go` of
    project `docs` is `wiki-go-docs`; a container outside compose uses its own name."""
    got = _owners([_row("CASA_WIKI", _ID_A, _compose("wiki-go", "docs")),
                   _row("loose_one", _ID_B, {})])
    assert got == {"routers": {"wiki-go-docs": "CASA_WIKI", "loose-one": "loose_one"},
                   "services": {"wiki-go-docs": "CASA_WIKI", "loose-one": "loose_one"}}


def test_an_empty_compose_project_label_still_counts_as_present():
    got = _owners([_row("CASA_X", _ID_A, _compose("svc", ""))])
    assert got["routers"] == {"svc": "CASA_X"}


def test_label_paths_match_case_insensitively_and_keep_the_names_case():
    got = _owners([_row("CASA_APP", _ID_A, {"traefik.HTTP.Routers.MyApp.rule": "Host(`a`)",
                                            "Traefik.http.services.MySvc.loadbalancer.server.port": "80"})])
    assert got == {"routers": {"MyApp": "CASA_APP"}, "services": {"MySvc": "CASA_APP"}}


@pytest.mark.parametrize("key,value", [("traefik.enable", "False"), ("traefik.enable", "0"),
                                       ("traefik.Enable", "false"), ("TRAEFIK.ENABLE", "f")])
def test_a_disabled_container_declares_nothing(key, value):
    """Traefik matches the key case-insensitively and reads the value with ParseBool."""
    got = _owners([_row("CASA_OFF", _ID_A, _compose("off", "p", **{key: value}))])
    assert got == {"routers": {}, "services": {}}


def test_a_one_off_compose_run_makes_its_services_routers_ambiguous_while_it_runs():
    """`docker compose run web migrate` copies web's labels and Traefik load-balances to it
    too, so for as long as it runs, which container `web` means is not one answer."""
    labels = _compose("web", "p", **{"traefik.http.routers.web.rule": "Host(`w`)"})
    got = _owners([_row("CASA_WEB", _ID_A, labels),
                   _row("p-web-run-1", _ID_B, {**labels, "com.docker.compose.oneoff": "True"})])
    assert "web" not in got["routers"]


def test_a_one_off_alone_owns_nothing():
    got = _owners([_row("p-job-run-1", _ID_A, _compose("job", "p", **{
        "com.docker.compose.oneoff": "True", "traefik.http.routers.job.rule": "Host(`j`)"}))])
    assert got == {"routers": {}, "services": {}}


def test_a_server_url_label_in_another_case_still_withholds_its_service():
    got = _owners([_row("CASA_P", _ID_A, {
        "traefik.http.routers.Foo.rule": "Host(`f`)", "traefik.http.routers.Foo.service": "Foo",
        "traefik.http.services.foo.loadbalancer.server.url": "http://10.0.0.9",
        "traefik.http.services.Foo.loadbalancer.passhostheader": "true"})])
    assert "Foo" not in got["services"] and "foo" not in got["services"]


def test_a_service_that_names_its_own_server_url_is_withheld():
    """It forwards wherever the URL says, so its router has no honest row."""
    got = _owners([_row("CASA_TRAEFIK", _ID_A, {
        "traefik.http.routers.nas.rule": "Host(`nas`)",
        "traefik.http.services.nas.loadBalancer.server.url": "http://192.168.1.171:5000"})])
    assert got["routers"] == {"nas": "CASA_TRAEFIK"}
    assert "nas" not in got["services"]


def test_a_container_is_disabled_only_when_every_enable_spelling_says_so():
    """Traefik merges case-variant keys and one wins; mixed means still a claimant."""
    mixed = _owners([_row("CASA_B", _ID_A, {"traefik.Enable": "false", "traefik.enable": "true",
                                            "traefik.http.routers.app.rule": "Host(`b`)"}),
                     _row("CASA_A", _ID_B, {"traefik.http.routers.app.rule": "Host(`a`)"})])
    assert "app" not in mixed["routers"]


def test_a_tcp_only_container_gets_no_default_http_router():
    got = _owners([_row("CASA_DNS", _ID_A, _compose("dns", "net", **{
        "traefik.tcp.routers.dns.rule": "HostSNI(`*`)"}))])
    assert got == {"routers": {}, "services": {}}


def test_a_tcp_middleware_alone_does_not_make_a_container_tcp_only():
    got = _owners([_row("CASA_X", _ID_A, _compose("x", "p", **{
        "traefik.tcp.middlewares.m.ipallowlist.sourcerange": "10.0.0.0/8"}))])
    assert got["routers"] == {"x-p": "CASA_X"}


def test_an_http_middleware_keeps_a_tcp_container_http_too():
    got = _owners([_row("CASA_X", _ID_A, _compose("x", "p", **{
        "traefik.tcp.routers.t.rule": "HostSNI(`*`)",
        "traefik.http.middlewares.m.headers.customrequestheaders.a": "b"}))])
    assert got["routers"] == {"x-p": "CASA_X"}


def test_two_services_and_no_router_is_no_default_router():
    """Traefik refuses to guess which of two services a default router would forward to."""
    got = _owners([_row("CASA_X", _ID_A, _compose("x", "p", **{
        "traefik.http.services.a.loadbalancer.server.port": "1",
        "traefik.http.services.b.loadbalancer.server.port": "2"}))])
    assert got == {"routers": {}, "services": {"a": "CASA_X", "b": "CASA_X"}}


def test_a_vpn_d_app_labelled_on_itself_owns_its_router():
    got = _owners([_row("CASA_GLUETUN", _ID_A, _compose("gluetun", "vpn")),
                   _row("CASA_QBIT", _ID_B, _compose("qbit", "vpn", **{
                       "traefik.http.routers.qbit.rule": "Host(`q`)",
                       "traefik.http.services.qbit.loadbalancer.server.port": "8080"}),
                        mode=f"container:{_ID_A}")])
    assert got["routers"] == {"qbit": "CASA_QBIT"}
    assert got["services"] == {"qbit": "CASA_QBIT"}


@pytest.mark.parametrize("ref", [_ID_A, _ID_A[:12], _ID_A[:40]])
def test_a_namespace_owner_declares_nothing_because_its_labels_may_be_its_sharers(ref):
    """The usual gluetun setup puts qbit's labels ON gluetun: router and service both gluetun's.
    qbit's button on gluetun's row would be wrong; `links:` places it."""
    got = _owners([_row("CASA_GLUETUN", _ID_A, _compose("gluetun", "vpn", **{
                       "traefik.http.routers.qbit.rule": "Host(`q`)",
                       "traefik.http.services.qbit.loadbalancer.server.port": "8080"})),
                   _row("CASA_QBIT", _ID_B, _compose("qbit", "vpn"), mode=f"container:{ref}")])
    assert "qbit" not in got["routers"] and "qbit" not in got["services"]
    assert "gluetun-vpn" not in got["routers"]


def test_a_namespace_owner_still_makes_a_shared_name_ambiguous():
    got = _owners([_row("CASA_GLUETUN", _ID_A, {"traefik.http.routers.web.rule": "Host(`g`)"}),
                   _row("CASA_QBIT", _ID_B, {}, mode=f"container:{_ID_A}"),
                   _row("CASA_WEB", _ID_C, {"traefik.http.routers.web.rule": "Host(`w`)"})])
    assert "web" not in got["routers"]


def test_a_sharer_naming_an_owner_that_no_longer_exists_fails_the_read():
    """gluetun recreated alone: qbit still names the old id, and the NEW gluetun -- carrying
    qbit's labels -- cannot be told apart from an ordinary container. Refused, not trusted."""
    with pytest.raises(ValueError):
        _owners([_row("CASA_GLUETUN", _ID_A, {"traefik.http.routers.qbit.rule": "Host(`q`)"}),
                 _row("CASA_QBIT", _ID_B, {}, mode="container:" + "f" * 64)])


def test_a_sharer_naming_its_owner_by_name_fails_the_read():
    """Docker resolves a name only at start; after a rename it can name another container."""
    with pytest.raises(ValueError):
        _owners([_row("vpn", _ID_A, {}), _row("CASA_QBIT", _ID_B, {}, mode="container:vpn")])


@pytest.mark.parametrize("kind", ["weighted.services[0].name", "mirroring.service",
                                  "failover.service"])
def test_a_service_that_forwards_to_other_services_is_withheld(kind):
    got = _owners([_row("CASA_A", _ID_A, {"traefik.http.routers.app.rule": "Host(`a`)",
                                          "traefik.http.routers.app.service": "app",
                                          f"traefik.http.services.app.{kind}": "app-b@docker"})])
    assert "app" not in got["services"]


def test_a_stopped_sharer_still_marks_its_owner():
    """qbit exited; Traefik can still list qbit's router, labelled on gluetun."""
    got = _owners([_row("CASA_GLUETUN", _ID_A, {"traefik.http.routers.qbit.rule": "Host(`q`)",
                                                "traefik.http.services.qbit.loadbalancer.server.port": "80"}),
                   _row("CASA_QBIT", _ID_B, {}, mode=f"container:{_ID_A}", running=False)])
    assert "qbit" not in got["routers"] and "qbit" not in got["services"]


def test_a_stopped_container_declares_nothing_and_blocks_nothing():
    """Traefik ignores stopped containers, so an old exited copy with the same labels must not
    make the running one's router ambiguous."""
    labels = {"traefik.http.routers.app.rule": "Host(`a`)"}
    got = _owners([_row("CASA_APP", _ID_A, labels), _row("CASA_APP_OLD", _ID_B, labels, running=False)])
    assert got["routers"] == {"app": "CASA_APP"}


def test_an_ambiguous_namespace_owner_prefix_fails_the_read():
    with pytest.raises(ValueError):
        _owners([_row("A", "ab" + "0" * 62, {}), _row("B", "ab" + "1" * 62, {}),
                 _row("Q", _ID_Q_ALT, {}, mode="container:ab")])


def test_a_name_two_containers_declare_belongs_to_neither():
    web = {"traefik.http.routers.web.rule": "Host(`w`)",
           "traefik.http.services.web.loadbalancer.server.port": "80"}
    got = _owners([_row("CASA_WEB_1", _ID_A, web), _row("CASA_WEB_2", _ID_B, web),
                   _row("CASA_OTHER", _ID_C, {"traefik.http.routers.other.rule": "Host(`o`)"})])
    assert got["routers"] == {"other": "CASA_OTHER"}
    assert "web" not in got["services"]


@pytest.mark.parametrize("name,expected", [
    ("wiki-go_docs", "wiki-go-docs"), ("__a..b__", "a-b"), ("SabNZBD", "SabNZBD"), ("___", "")])
def test_traefik_normalise_matches_provider_normalize(name, expected):
    assert actions.traefik_normalise(name) == expected


@pytest.mark.parametrize("out,ids", [
    ("junk", [_ID_A]),                                                  # malformed
    ("/X\tnothex\t{}", ["nothex"]),                                    # bad id
    (f"/X\t{_ID_A}\trunning\tbridge\t[1]", [_ID_A]),                   # labels not a map
    (f"/X\t{_ID_A}\trunning\tbridge\t{{bad json", [_ID_A]),            # labels unreadable
    (f"/X\t{_ID_A}\tbridge\t{{}}", [_ID_A]),                          # a field short
    (f"/X\t{_ID_A}\t\tbridge\t{{}}", [_ID_A]),                        # no status
    (_row("X", _ID_A, {}), [_ID_A, _ID_B]),                             # a container lost
    (_row("X", _ID_A, {}) + "\n" + _row("Y", _ID_B, {}), [_ID_A]),     # one extra
])
def test_anything_short_of_a_complete_read_is_refused(out, ids):
    """A container missing from the read would leave a name it also declares looking unique."""
    with pytest.raises((ValueError, TypeError)):
        actions.parse_router_owners(out, ids)


def test_null_labels_are_no_labels():
    assert _owners([f"/plain\t{_ID_A}\trunning\tbridge\tnull"])["routers"] == {"plain": "plain"}


@pytest.mark.parametrize("status", ["paused", "restarting", "created", "exited", "dead"])
def test_only_a_running_container_owns_anything(status):
    got = _owners([_row("CASA_X", _ID_A, {"traefik.http.routers.x.rule": "Host(`x`)"},
                        status=status)])
    assert got["routers"] == {}


def test_the_read_reports_status_the_same_way_the_listing_does():
    """A paused container must key identically from both sides, or every read is a race."""
    got = actions.parse_router_owners(_row("CASA_X", _ID_A, {}, status="paused"), [_ID_A])
    assert got["containers"] == [[_ID_A, "CASA_X", "paused"]]


def test_a_label_value_with_a_unicode_line_separator_does_not_split_the_record():
    """Go's JSON leaves U+0085 unescaped; str.splitlines() would break the line on it."""
    got = _owners([_row("CASA_X", _ID_A, {"note": "a\x85b\u2028c",
                                          "traefik.http.routers.x.rule": "Host(`x`)"})])
    assert got["routers"] == {"x": "CASA_X"}


def test_the_read_reports_the_names_it_saw_for_the_cache_key():
    out = "\n".join([_row("CASA_B", _ID_B, {}, running=False), _row("CASA_A", _ID_A, {})])
    got = actions.parse_router_owners(out, [_ID_A, _ID_B])
    assert got["containers"] == [[_ID_A, "CASA_A", "running"], [_ID_B, "CASA_B", "exited"]]


def _docker(monkeypatch, answer):
    calls = []

    def run(argv, timeout):
        calls.append((argv, timeout))
        return answer
    monkeypatch.setattr(actions.bender, "run_argv", run)
    return calls


def test_list_containers_is_every_container_with_whether_it_runs(monkeypatch):
    calls = _docker(monkeypatch, (0, f"{_ID_B}\tCASA_B\texited\n{_ID_A}\tCASA_A\trunning", ""))
    assert actions.list_containers(timeout=4) == {
        "ok": True, "containers": [[_ID_A, "CASA_A", "running"], [_ID_B, "CASA_B", "exited"]]}
    assert calls[0][0] == ["docker", "ps", "-a", "--no-trunc", "--format",
                           "{{.ID}}\t{{.Names}}\t{{.State}}"]


def test_legacy_link_aliases_resolve_to_the_containers_own_name(monkeypatch):
    _docker(monkeypatch, (0, f"{_ID_A}\tapp,web/app\trunning", ""))
    assert actions.list_containers(timeout=4) == {"ok": True, "containers": [[_ID_A, "app", "running"]]}


@pytest.mark.parametrize("answer,error", [
    ((124, "", "timed out"), "timeout"),
    ((1, "", "daemon down"), "unavailable"),
    ((0, "--format=evil\tx\trunning", ""), "unavailable"),  # never passed on as an argument
    ((0, f"{_ID_A}\t\trunning", ""), "unavailable"),        # no name
    ((0, f"{_ID_A}\ta,b\trunning", ""), "unavailable"),     # two own names: not a shape we know
    ((0, f"{_ID_A}\ta", ""), "unavailable"),                 # a field short
])
def test_list_containers_failures_are_typed(monkeypatch, answer, error):
    _docker(monkeypatch, answer)
    assert actions.list_containers(timeout=4) == {"ok": False, "error": error}


def test_read_router_owners_is_one_inspect_limited_to_containers(monkeypatch):
    calls = _docker(monkeypatch, (0, _row("CASA_X", _ID_A, {"traefik.http.routers.x.rule": "H"}), ""))
    assert actions.read_router_owners([_ID_A], timeout=4) == {
        "ok": True, "routers": {"x": "CASA_X"}, "services": {"CASA-X": "CASA_X"},
        "containers": [[_ID_A, "CASA_X", "running"]], "icons": {}}
    assert calls[0][0] == ["docker", "inspect", "--type", "container",
                           "--format", actions.ROUTERS_FORMAT, _ID_A]


@pytest.mark.parametrize("ids,answer,expected", [
    ([_ID_A], (124, "", "timed out"), {"error": "timeout"}),
    # A container removed since the listing: churn, which the caller may retry once for free.
    ([_ID_A, _ID_B], (1, _row("X", _ID_A, {}), "Error: No such container: bbbb"),
     {"error": "unavailable", "race": True}),
    # Anything else will repeat: no free retry.
    ([_ID_A], (1, "", "permission denied while trying to connect"), {"error": "unavailable"}),
    ([_ID_A], (0, "junk", ""), {"error": "unavailable"}),
    (["--format=x"], None, {"error": "unavailable"}),
    # An owner that cannot be identified will not be identified by reading again.
    ([_ID_A, _ID_B], (0, _row("vpn", _ID_A, {}) + "\n" +
                      _row("Q", _ID_B, {}, mode="container:vpn"), ""),
     {"error": "unavailable", "final": True}),
])
def test_read_router_owners_failures_are_typed(monkeypatch, ids, answer, expected):
    _docker(monkeypatch, answer)
    assert actions.read_router_owners(ids, timeout=4) == {"ok": False, **expected}


def test_undecodable_docker_output_is_a_typed_failure(monkeypatch):
    def run(argv, timeout):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    monkeypatch.setattr(actions.bender, "run_argv", run)
    assert actions.list_containers(timeout=4) == {"ok": False, "error": "unavailable"}
    assert actions.read_router_owners([_ID_A], timeout=4) == {"ok": False, "error": "unavailable"}


def test_a_short_hex_ref_is_never_taken_as_an_id_prefix():
    """`container:db` after `docker rename db postgres`: no container is named db any more,
    and an unrelated id that starts `db` must not be taken for the owner."""
    with pytest.raises(ValueError):
        _owners([_row("postgres", "2" * 64, {"traefik.http.routers.app.rule": "Host(`a`)"}),
                 _row("other", "db" + "1" * 62, {}),
                 _row("app", _ID_C, {}, mode="container:db")])


def test_a_hex_looking_name_resolves_as_a_name_first():
    """Docker resolves `container:db` to the container NAMED db before any id prefix."""
    with pytest.raises(ValueError):
        _owners([_row("db", "2" * 64, {"traefik.http.routers.qbit.rule": "Host(`q`)"}),
                 _row("other", "db" + "1" * 62, {}),
                 _row("sharer", _ID_C, {}, mode="container:db")])


@pytest.mark.parametrize("status", ["paused", "restarting"])
def test_a_paused_or_restarting_replica_keeps_a_shared_name_ambiguous(status):
    """Traefik still lists it, so which replica a button would open is not one answer."""
    labels = {"traefik.http.routers.web.rule": "Host(`w`)"}
    got = _owners([_row("web_1", _ID_A, labels), _row("web_2", _ID_B, labels, status=status)])
    assert "web" not in got["routers"]


def test_no_containers_or_no_budget_runs_nothing(monkeypatch):
    calls = _docker(monkeypatch, None)
    assert actions.read_router_owners([], timeout=4) == {
        "ok": True, "routers": {}, "services": {}, "containers": []}
    assert actions.read_router_owners([_ID_A], timeout=0) == {"ok": False, "error": "timeout"}
    assert actions.list_containers(timeout=0) == {"ok": False, "error": "timeout"}
    assert calls == []




# ── Where a container's widget API is (T46.3) ──────────────────────────────────

_IMG = "sha256:" + "e" * 64
_NET_BRIDGE, _NET_MACVLAN = "1" * 64, "2" * 64


def _networks(**by_name):
    return {name: {"NetworkID": net, "IPAddress": ip} for name, (net, ip) in by_name.items()}


def _widget_line(labels, networks=None, status="running", mode="default",
                 image="lscr.io/linuxserver/sonarr:latest"):
    networks = _networks(media=(_NET_BRIDGE, "172.18.0.5")) if networks is None else networks
    return (f"{_ID_A}\t{_IMG}\t{image}\t{status}\t{mode}\t{json.dumps(labels)}\t"
            f"{json.dumps(networks)}")


_DRIVERS = (0, f"{_NET_BRIDGE}\tbridge\n{_NET_MACVLAN}\tmacvlan", "")


_PULLED = (0, '{}\t["lscr.io/linuxserver/sonarr@sha256:' + "f" * 64 + '"]', "")


def _widget_docker(monkeypatch, container_answer, image_answer=_PULLED, owner_answer=None,
                   drivers=_DRIVERS):
    calls = []

    def run(argv, timeout):
        calls.append(argv)
        if argv[1] == "image":
            return image_answer
        if argv[1] == "network":
            return drivers
        if argv[-1] != "CASA_SONARR" and argv[-1] != "CASA_X":
            return owner_answer
        return container_answer
    monkeypatch.setattr(actions.bender, "run_argv", run)
    return calls


def test_widget_target_reads_the_container_and_its_networks_drivers(monkeypatch):
    calls = _widget_docker(monkeypatch, (0, _widget_line({}), ""))
    got = actions.read_widget_target("CASA_SONARR", timeout=4)
    assert got == {"ok": True, "id": _ID_A, "image": "lscr.io/linuxserver/sonarr:latest",
                   "running": True, "label": None, "label_from_image": False,
                   "pulled_from": ["lscr.io/linuxserver/sonarr"], "shares_namespace": False,
                   "addresses": ["172.18.0.5"]}
    assert calls[0] == ["docker", "inspect", "--type", "container", "--format",
                        actions.WIDGET_TARGET_FORMAT, "CASA_SONARR"]
    assert calls[1] == ["docker", "image", "inspect", "--format",
                        "{{json .Config.Labels}}\t{{json .RepoDigests}}", _IMG]
    assert calls[2] == ["docker", "network", "inspect", "--format", "{{.Id}}\t{{.Driver}}",
                        _NET_BRIDGE]


@pytest.mark.parametrize("digests,pulled", [
    ("null", []), ("[]", []), ('["evil.example/linuxserver/sonarr@sha256:' + "f" * 64 + '"]',
                               ["evil.example/linuxserver/sonarr"]),
    ('["junk"]', []), ("[1]", [])])
def test_where_an_image_was_pulled_from_is_read_from_its_registry_digests(monkeypatch, digests, pulled):
    """A local build has no digest; a foreign registry's digest names that registry."""
    _widget_docker(monkeypatch, (0, _widget_line({}), ""), image_answer=(0, "{}\t" + digests, ""))
    assert actions.read_widget_target("CASA_X", timeout=4)["pulled_from"] == pulled


def test_only_bridge_network_addresses_are_handed_over(monkeypatch):
    networks = _networks(casapilan=(_NET_MACVLAN, "192.168.1.53"), web=(_NET_BRIDGE, "172.20.0.4"))
    _widget_docker(monkeypatch, (0, _widget_line({}, networks=networks), ""))
    assert actions.read_widget_target("CASA_X", timeout=4)["addresses"] == ["172.20.0.4"]


def test_a_network_whose_driver_cannot_be_read_hands_over_nothing(monkeypatch):
    _widget_docker(monkeypatch, (0, _widget_line({}), ""), drivers=(1, "", "no such network"))
    assert actions.read_widget_target("CASA_X", timeout=4) == {"ok": False, "error": "unavailable"}


def test_a_namespace_sharer_uses_its_owners_bridge_address(monkeypatch):
    """qbittorrent in gluetun's namespace: its API is on gluetun's addresses."""
    owner = (0, f"{_ID_B}\t{json.dumps(_networks(vpn=(_NET_BRIDGE, '172.30.0.2')))}", "")
    calls = _widget_docker(monkeypatch, (0, _widget_line({}, networks={}, mode=f"container:{_ID_B}"), ""),
                           owner_answer=owner)
    assert actions.read_widget_target("CASA_X", timeout=4)["addresses"] == ["172.30.0.2"]
    assert calls[2][-1] == _ID_B


def test_a_label_set_on_the_container_is_the_operators(monkeypatch):
    _widget_docker(monkeypatch, (0, _widget_line({"planetexpress.widget": "sonarr"}), ""),
                   (0, json.dumps({"maintainer": "x"}) + "\t[]", ""))
    got = actions.read_widget_target("CASA_X", timeout=4)
    assert got["label"] == "sonarr" and got["label_from_image"] is False


def test_a_label_baked_into_the_image_is_flagged(monkeypatch):
    _widget_docker(monkeypatch, (0, _widget_line({"planetexpress.widget": "sonarr"}), ""),
                   (0, json.dumps({"planetexpress.widget": "sonarr"}) + "\t[]", ""))
    assert actions.read_widget_target("CASA_X", timeout=4)["label_from_image"] is True


@pytest.mark.parametrize("image_answer", [(1, "", "no such image"), (0, "junk\t[]", ""), (0, "[1]\t[]", "")])
def test_an_unreadable_image_label_counts_as_the_images(monkeypatch, image_answer):
    """Cannot tell where the label came from: the safe reading."""
    _widget_docker(monkeypatch, (0, _widget_line({"planetexpress.widget": "sonarr"}), ""), image_answer)
    assert actions.read_widget_target("CASA_X", timeout=4)["label_from_image"] is True


def test_a_host_networked_container_has_no_addresses(monkeypatch):
    calls = _widget_docker(monkeypatch, (0, _widget_line({}, networks={"host": {"NetworkID": _NET_BRIDGE,
                                                                               "IPAddress": ""}}), ""))
    assert actions.read_widget_target("CASA_X", timeout=4)["addresses"] == []
    assert all(c[1] != "network" for c in calls)


@pytest.mark.parametrize("container,answer,error", [
    ("--format=x", None, "unavailable"),
    ("CASA_X", (124, "", "timed out"), "timeout"),
    ("CASA_X", (1, "", "No such container"), "unavailable"),
    ("CASA_X", (0, "junk", ""), "unavailable"),
    ("CASA_X", (0, f"nothex\t{_IMG}\timg\trunning\tdefault\t{{}}\t{{}}", ""), "unavailable"),
    ("CASA_X", (0, f"{_ID_A}\t{_IMG}\timg\trunning\tdefault\t{{}}\t[1]", ""), "unavailable"),
])
def test_widget_target_failures_are_typed(monkeypatch, container, answer, error):
    _widget_docker(monkeypatch, answer)
    assert actions.read_widget_target(container, timeout=4) == {"ok": False, "error": error}



def test_a_sharer_naming_its_owner_by_name_gets_no_addresses(monkeypatch):
    """A name is resolved when asked; after a rename it names another container."""
    calls = _widget_docker(monkeypatch, (0, _widget_line({}, networks={}, mode="container:gluetun"), ""))
    assert actions.read_widget_target("CASA_X", timeout=4)["addresses"] == []
    assert all(c[1] != "inspect" or c[-1] == "CASA_X" for c in calls)


def test_an_owner_that_answers_with_another_id_gets_no_addresses(monkeypatch):
    owner = (0, f"{_ID_C}\t{json.dumps(_networks(vpn=(_NET_BRIDGE, '172.30.0.2')))}", "")
    _widget_docker(monkeypatch, (0, _widget_line({}, networks={}, mode=f"container:{_ID_B}"), ""),
                   owner_answer=owner)
    assert actions.read_widget_target("CASA_X", timeout=4)["addresses"] == []


def test_an_owner_that_no_longer_exists_gets_no_addresses_not_an_error(monkeypatch):
    """Recreated alone: a stale id. 'Host slow, retry' would never succeed."""
    _widget_docker(monkeypatch, (0, _widget_line({}, networks={}, mode=f"container:{_ID_B}"), ""),
                   owner_answer=(1, "", "No such container"))
    got = actions.read_widget_target("CASA_X", timeout=4)
    assert got["ok"] is True and got["addresses"] == []


# ── the app-icon override label (T46.7) ─────────────────────────────────────────

def _icons(rows, ids=None):
    if ids is None:
        ids = [row.split("\t")[1] for row in rows]
    return actions.parse_router_owners("\n".join(rows).strip(), ids)["icons"]


def test_the_icon_label_is_carried_from_the_labels_already_read():
    assert _icons([_row("CASA_X", _ID_A, {"planetexpress.icon": "plex"})]) == {"CASA_X": "plex"}


def test_a_container_without_the_icon_label_is_absent_rather_than_empty():
    """Absent, so slug_for() falls through to the image. An empty string would read as a
    declared value and is not one."""
    assert _icons([_row("CASA_X", _ID_A, {"traefik.enable": "true"})]) == {}


def test_the_icon_label_survives_a_value_that_would_break_a_format_string():
    """The reason this lives here and not in `docker ps --format`: {{json .Config.Labels}}
    encodes a quote or a tab, a per-label format string does not, and a container whose line
    fails to parse would drop out of the scan altogether."""
    hostile = 'he said "no"\tand a tab'
    got = _icons([_row("CASA_X", _ID_A, {"planetexpress.icon": hostile})])
    assert got == {"CASA_X": hostile}


def test_a_non_string_icon_label_is_ignored():
    """Docker labels are strings, but this parses JSON someone else produced."""
    assert _icons([_row("CASA_X", _ID_A, {"planetexpress.icon": None})]) == {}


def test_the_icon_map_uses_the_name_the_snapshot_uses():
    """`docker inspect` reports .Name with a leading slash; `docker ps --format {{.Names}}`,
    which is what the monitor snapshot holds, does not. The icon map is joined to the
    snapshot by name, so it must carry the slashless form -- an off-by-one-slash here would
    silently ignore every real `planetexpress.icon` label while slashless test fixtures passed.
    """
    cid = "a" * 64
    row = "\t".join(["/CASA_X", cid, "running", "default",
                     json.dumps({"planetexpress.icon": "plex"})])
    assert actions.parse_router_owners(row, [cid])["icons"] == {"CASA_X": "plex"}
