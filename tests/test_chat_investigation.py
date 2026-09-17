"""Provider-backed chat contracts: prose is never diagnostic evidence."""
import json
from unittest.mock import Mock

import pytest
from test_diagnostic_evidence import (
    FABRICATED,
    _an,
    _an_text,
    _an_tool,
    _install_anthropic,
    _install_openai,
    _oa_calls,
    _oa_text,
)

import casa_farnsworth as f
from planet_express.application.command_service import ProposeResult


@pytest.fixture(params=['openai', 'anthropic'])
def provider(request, monkeypatch):
    monkeypatch.setattr(f.bender, 'run_diagnostic', lambda cmd: (0, 'real output', ''))
    if request.param == 'openai':
        return lambda script: _install_openai(monkeypatch, script), _oa_calls, _oa_text
    return (lambda script: _install_anthropic(monkeypatch, script),
            lambda *cmds: _an(*[_an_tool(cmd, i) for i, cmd in enumerate(cmds)]),
            lambda text: _an(_an_text(text)))


def final(outcome='answer', cited=None, answer='Observed running.', proposal=None):
    return json.dumps({"outcome": outcome, "answer": answer, "cited_evidence": [0] if cited is None else cited,
                       "proposal": proposal})


def investigate(reserve=lambda: True, commands=None):
    return f._run_chat_investigation('What failed?', reserve, operator='chris', commands=commands or Mock())


def test_answer_and_fabricated_prose_stays_out_of_evidence(provider):
    install, calls, text = provider
    requests = install([calls('docker ps'), text(final(answer=FABRICATED))])
    result = investigate()
    assert result.outcome == 'answer' and result.cited == [0]
    assert result.answer == FABRICATED
    assert result.evidence == [{'command': 'docker ps', 'exit_code': 0, 'stdout': 'real output', 'stderr': ''}]
    assert all(r['tools'] for r in requests)


@pytest.mark.parametrize('cited', [[], [-1, 1, True, '0', 0.0], [999]])
def test_missing_valid_citations(provider, cited):
    install, calls, text = provider
    install([calls('docker ps'), text(final(cited=cited))])
    result = investigate()
    assert result.outcome == 'insufficient_evidence' and result.answer == 'Observed running.'


@pytest.mark.parametrize('action,ok', [('docker.restart_service', True), ('docker.restart_service', False),
                                      ('unknown', True), ('docker.stats_service', True)])
def test_proposal(provider, action, ok):
    install, calls, text = provider
    install([calls('docker ps'), text(final('proposal', proposal={'action': action, 'stack': 's', 'service': 'web'}))])
    commands = Mock()
    commands.propose.return_value = ProposeResult(ok, 'approval', True, 'cooling down')
    result = investigate(commands=commands)
    if action == 'docker.restart_service':
        commands.propose.assert_called_once_with(action, 's', 'web', requested_via='chat', requested_by='chris')
        assert result.outcome == ('proposal' if ok else 'unsupported_fix')
        assert result.approval_id == ('approval' if ok else None)
        if not ok:
            assert 'cooling down' in result.answer
    else:
        commands.propose.assert_not_called()
        assert result.outcome == 'unsupported_fix'


@pytest.mark.parametrize('value', ['not JSON', '{}', '[]', '{"outcome":NaN}',
                                     final().replace('"proposal": null', '"proposal": null, "extra": 1')])
def test_invalid_contract(provider, value):
    install, _, text = provider
    install([text(value)])
    result = investigate()
    assert result.status == 'failed' and result.error == 'model returned an invalid answer'


@pytest.mark.parametrize('allowed', [0, 1])
def test_quota_stops_requests(provider, allowed):
    install, calls, text = provider
    requests = install([calls('docker ps'), text(final())])
    reservations = iter([True] * allowed + [False])
    result = investigate(reserve=lambda: next(reservations))
    assert result.outcome == 'quota_exhausted'
    assert len(requests) == allowed and len(result.evidence) == allowed


@pytest.mark.parametrize('more_tools', [False, True])
def test_budget_one_extra_request_with_tools(provider, more_tools):
    install, calls, text = provider
    requests = install([calls(*(['docker ps'] * f.MAX_DIAGNOSTIC_ROUNDS)),
                        calls('docker ps') if more_tools else text(final())])
    reserve = Mock(return_value=True)
    result = investigate(reserve=reserve)
    assert len(requests) == reserve.call_count == 2
    assert all(r['tools'] for r in requests)
    assert len(result.evidence) == f.MAX_DIAGNOSTIC_ROUNDS
    assert result.outcome == ('insufficient_evidence' if more_tools else 'answer')


def test_fence_redaction_and_truncation(provider):
    install, calls, text = provider
    install([calls('docker ps'), text('```json\n' + final(answer='API_KEY=secret\n' + 'x' * 5000) + '\n```')])
    result = investigate()
    assert result.outcome == 'answer'
    assert len(result.answer) == 4000 and 'secret' not in result.answer


def test_no_tool_result_cannot_support_answer(provider):
    install, _, text = provider
    install([text(final())])
    result = investigate()
    assert result.outcome == 'insufficient_evidence' and result.evidence == []


def test_extra_final_request_also_reserves_quota(provider):
    install, calls, _ = provider
    requests = install([calls(*(['docker ps'] * f.MAX_DIAGNOSTIC_ROUNDS))])
    reservations = iter([True, False])
    result = investigate(reserve=lambda: next(reservations))
    assert result.outcome == 'quota_exhausted'
    assert len(requests) == 1 and len(result.evidence) == f.MAX_DIAGNOSTIC_ROUNDS


def test_last_turn_only_and_serial_budget(provider):
    install, calls, text = provider
    requests = install([calls('docker ps') for _ in range(f.MAX_DIAGNOSTIC_ROUNDS)] + [text(final())])
    result = investigate()
    assert result.outcome == 'answer'
    assert len(requests) == f.MAX_DIAGNOSTIC_ROUNDS + 1
    assert all(r['tools'] for r in requests)


def test_duplicate_json_key_is_invalid(provider):
    install, _, text = provider
    install([text(final().replace('{', '{"answer":"conflicting",', 1))])
    assert investigate().status == 'failed'
