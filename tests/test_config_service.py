import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from planet_express.application import config_service
from planet_express.application.config_service import ConfigService


class State:
    busy_reason = 'scan running'

    def __init__(self, busy=False):
        self.busy = busy
        self.owner = None
        self.attempts = []

    def try_begin_mutation(self, owner, *, require_idle):
        self.attempts.append((owner, require_idle))
        if self.busy:
            return False
        self.owner = owner
        return True

    def end_mutation(self, owner):
        assert self.owner == owner
        self.owner = None


@pytest.mark.parametrize('case', ['invalid', 'busy', 'success', 'write_failed', 'activate_raises'])
def test_apply(tmp_path, monkeypatch, case):
    path = tmp_path / 'config.yaml'
    path.write_text('original')
    state = State(busy=case == 'busy')
    activated = []
    draft = 'stacks_root: /srv\n'

    def activate():
        assert state.owner == 'config-apply'
        assert path.read_text() == draft
        activated.append(True)
        if case == 'activate_raises':
            raise RuntimeError('activation failed')

    def fail_write(*args):
        raise OSError('disk full')

    if case == 'write_failed':
        monkeypatch.setattr(config_service, 'write_config_text', fail_write)
    service = ConfigService(state, config_path=path, activate=activate)
    assert service.validate(draft).ok
    assert not service.validate('[]').ok
    assert state.attempts == []
    if case == 'activate_raises':
        with pytest.raises(RuntimeError, match='activation failed'):
            service.apply(draft)
    else:
        result = service.apply('[]' if case == 'invalid' else draft)
        assert result.status == ('activating' if case == 'success' else case)
        if case == 'invalid':
            assert result.errors
        if case == 'busy':
            assert result.reason == state.busy_reason
        if case == 'write_failed':
            assert result.reason == 'disk full'
    assert state.owner is None
    assert state.attempts == ([] if case == 'invalid' else [('config-apply', True)])
    assert activated == ([True] if case in ('success', 'activate_raises') else [])
    assert path.read_text() == (draft if activated else 'original')
