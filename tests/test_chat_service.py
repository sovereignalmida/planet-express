"""Ticket admission, ownership, failure isolation, and local calendar quota."""
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import Mock

import pytest

from planet_express.application.chat_service import (
    ChatResult,
    ChatService,
    local_day_start,
)
from planet_express.core.store import Store


class SyncExecutor:
    def submit(self, fn, *args):
        fn(*args)


@pytest.fixture
def store(tmp_path):
    result = Store(tmp_path / 'chat.db')
    result.init()
    return result


def service(store, run=None, **kwargs):
    return ChatService(store, Mock(), run_investigation=run or Mock(return_value=ChatResult(outcome='answer')),
                       executor=kwargs.pop('executor', SyncExecutor()), **kwargs)


def ask(chat, submission_id='abcdefgh', operator='chris', question='What failed?'):
    return chat.ask(operator=operator, question=question, submission_id=submission_id)


def test_queued_response_dedup_and_ownership(store):
    result = ChatResult(outcome='proposal', answer='Restart?', evidence=[{'stdout': 'real'}],
                        cited=[0], approval_id='approval')
    run = Mock(return_value=result)
    chat = service(store, run)
    first = ask(chat)
    assert first['status'] == 'queued'
    again = ask(chat)
    assert again['ticket_id'] == first['ticket_id']
    assert again['status'] == 'done'
    assert run.call_count == 1
    assert run.call_args.kwargs['operator'] == 'chris'
    row = chat.get(first['ticket_id'], operator='chris')
    assert row['evidence'] == result.evidence and row['approval_id'] == 'approval'
    assert row['cited'] == [0] and row['answer'] == 'Restart?'
    assert chat.get(first['ticket_id'], operator='other') is None
    assert chat.get('missing', operator='chris') is None


def test_busy_is_immediate_and_deduplicated(store):
    entered, release = threading.Event(), threading.Event()

    def run(question, **kwargs):
        entered.set()
        assert release.wait(5)
        return ChatResult(outcome='insufficient_evidence')

    with ThreadPoolExecutor(max_workers=1) as executor:
        chat = service(store, run, executor=executor)
        try:
            first = ask(chat)
            assert entered.wait(2)
            assert first['status'] == 'queued'
            for i in range(3):
                assert ask(chat, f'queued_{i}')['status'] == 'queued'
            refused = ask(chat, 'refused_1')
            assert refused['status'] == 'failed'
            assert refused['error'] == 'chat is busy, try again shortly'
            assert ask(chat, 'refused_1')['ticket_id'] == refused['ticket_id']
        finally:
            release.set()
    assert ask(chat, 'closed_1')['error'] == 'chat could not start'


@pytest.mark.parametrize('question,submission_id', [('', 'abcdefgh'), ('  ', 'abcdefgh'),
    ('x' * 2001, 'abcdefgh'), (None, 'abcdefgh'), ('ok', 'short'), ('ok', 'x' * 65),
    ('ok', 'invalid!'), ('ok', None)])
def test_validation(store, question, submission_id):
    with pytest.raises(ValueError):
        ask(service(store), question=question, submission_id=submission_id)


def test_exception_and_slot_release(store):
    chat = service(store, Mock(side_effect=RuntimeError('https://secret-key')))
    for i in range(6):
        ticket = ask(chat, f'failure{i}')
        row = chat.get(ticket['ticket_id'], operator='chris')
        assert row['status'] == 'failed'
        assert row['error'] == 'chat investigation failed'
        assert 'secret' not in str(row)


def test_worker_reservation_quota_and_reconcile(store):
    def run(question, *, reserve, operator):
        assert reserve()
        return ChatResult(outcome='quota_exhausted')

    chat = service(store, run)
    ticket = ask(chat)
    assert chat.get(ticket['ticket_id'], operator='chris')['llm_calls'] == 1
    quota = chat.quota()
    assert quota['used'] == 1 and quota['limit'] == 100
    assert quota['resets_at'] > time.time()
    store.create_chat_ticket(operator='chris', submission_id='unfinished', question='?')
    assert chat.reconcile_on_startup() == 1
    assert chat.reconcile_on_startup() == 0


@pytest.mark.parametrize('date', [(2026, 3, 29), (2026, 10, 25), (2026, 9, 17)])
def test_local_midnight_and_dst(store, date):
    previous = os.environ.get('TZ')
    os.environ['TZ'] = 'Europe/Lisbon'
    time.tzset()
    try:
        midnight = datetime(*date).astimezone().timestamp()
        noon = datetime(*date, 12).astimezone().timestamp()
        assert local_day_start(midnight) == midnight
        assert local_day_start(noon) == midnight
        assert local_day_start(midnight - 1) < midnight
        quota = service(store, clock=lambda: noon).quota()
        reset = datetime.fromtimestamp(quota['resets_at']).astimezone()
        assert reset.hour == 0 and reset.day == date[2] + 1
    finally:
        if previous is None:
            os.environ.pop('TZ', None)
        else:
            os.environ['TZ'] = previous
        time.tzset()


def test_worker_maps_failed_result(store):
    result = ChatResult(status='failed', error='model returned an invalid answer', evidence=[{'stdout': 'real'}])
    chat = service(store, Mock(return_value=result))
    ticket = ask(chat)
    row = chat.get(ticket['ticket_id'], operator='chris')
    assert row['status'] == 'failed' and row['error'] == result.error and row['evidence'] == result.evidence
