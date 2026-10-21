"""Finite public-SDK classification and independent child-ownership guards."""
import os
from pathlib import Path
from types import SimpleNamespace

import anyio
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, INTERNAL_ERROR, REQUEST_TIMEOUT
import pytest

from enterprise_ai_lab.chaos.transport import _owned, _pid, _transport_code


@pytest.mark.parametrize('exception,expected', [
    (MCPError(REQUEST_TIMEOUT, 'private text ignored'), 'transport_timeout'),
    (MCPError(CONNECTION_CLOSED, 'private text ignored'), 'transport_closed'),
    (anyio.EndOfStream(), 'transport_closed'),
    (MCPError(INTERNAL_ERROR, 'Connection closed'), None),
    (TimeoutError('not the work-budget scope'), None),
    (OSError('connection closed'), None),
    (RuntimeError('REQUEST_TIMEOUT'), None),
])
def test_transport_classification_uses_types_codes_not_messages(exception, expected):
    assert _transport_code(exception) == expected


def test_mixed_exception_groups_are_terminal():
    first = MCPError(CONNECTION_CLOSED, 'closed')
    assert _transport_code(ExceptionGroup('same', [first, first])) == 'transport_closed'
    assert _transport_code(ExceptionGroup('mixed', [first, RuntimeError('failure')])) is None
    assert _transport_code(ExceptionGroup('different', [first, MCPError(REQUEST_TIMEOUT, 'timeout')])) is None


@pytest.mark.parametrize('mode,content', [(0o644, '1234'), (0o600, '-1'), (0o600, '1'), (0o600, '1.0'), (0o600, '1234\n')])
def test_untrusted_pid_record_is_never_used(tmp_path, mode, content):
    record = tmp_path / 'owned.pid'
    record.write_text(content)
    record.chmod(mode)
    assert _pid(record) is None


def test_pid_record_hardlink_is_rejected(tmp_path):
    record = tmp_path / 'owned.pid'
    record.write_text('1234')
    record.chmod(0o600)
    os.link(record, tmp_path / 'alias')
    assert _pid(record) is None


def test_pid_requires_matching_parent_and_unique_record_argument(tmp_path, monkeypatch):
    record = tmp_path / 'owned.pid'
    record.write_text('1234')
    record.chmod(0o600)
    class Result:
        returncode = 0
        stdout = f'{os.getpid()} python enterprise_ai_lab.mcp_gateway other.pid'
    monkeypatch.setattr('enterprise_ai_lab.chaos.transport.subprocess.run', lambda *args, **kwargs: Result())
    assert _owned(record) is None
    Result.stdout = f'{os.getpid() + 1} python enterprise_ai_lab.mcp_gateway {record}'
    assert _owned(record) is None
    Result.stdout = f'{os.getpid()} python enterprise_ai_lab.mcp_gateway {record}'
    assert _owned(record) == 1234
