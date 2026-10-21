"""Actual official SDK transport with independently observed owned-child cleanup."""
from __future__ import annotations
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

import anyio
from mcp import Client, StdioServerParameters
from mcp.shared.exceptions import MCPError
from mcp.types import CONNECTION_CLOSED, REQUEST_TIMEOUT

from .client import AttemptFailure
from .contract import ReadFailure
from ..mcp_gateway.audit import ERRORS

# Fixed launcher only; no caller-controlled command, SQL, transport or role.
_BOOTSTRAP = '''import os,runpy,sys
fd=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as stream: stream.write(str(os.getpid()))
sys.argv=sys.argv[2:]
runpy.run_module('enterprise_ai_lab.mcp_gateway',run_name='__main__')
'''


def _pid(record):
    try:
        info = record.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_uid != os.getuid() or not 1 <= info.st_size <= 20):
            return None
        value = record.read_text()
        return int(value) if value.isascii() and value.isdecimal() and int(value) > 1 else None
    except (OSError, UnicodeError, ValueError):
        return None


def _owned(record):
    pid = _pid(record)
    if pid is None:
        return None
    try:
        found = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'ppid=,args='],
                               capture_output=True, text=True, timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return None
    row = found.stdout.strip().split(None, 1)
    if (found.returncode == 0 and len(row) == 2 and row[0] == str(os.getpid())
            and str(record) in row[1] and 'enterprise_ai_lab.mcp_gateway' in row[1]):
        return pid
    return None


def _kill_owned(record):
    pid = _owned(record)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _transport_code(exc):
    # SDK task groups can wrap the actual public session error. Mixed/unknown
    # groups are terminal; no message/class-name matching or broad OSError retry.
    if isinstance(exc, BaseExceptionGroup):
        codes = [_transport_code(child) for child in exc.exceptions]
        return codes[0] if codes and codes[0] is not None and len(set(codes)) == 1 else None
    if isinstance(exc, MCPError):
        return {REQUEST_TIMEOUT: 'transport_timeout', CONNECTION_CLOSED: 'transport_closed'}.get(exc.code)
    if type(exc) in (anyio.EndOfStream, anyio.BrokenResourceError, anyio.ClosedResourceError):
        return 'transport_closed'
    return None


class StdioDriver:
    def __init__(self):
        self.records = []

    def _bootstrap(self, endpoint):
        return _BOOTSTRAP

    async def _before_call(self, endpoint, client, record):
        pass

    async def attempt(self, endpoint, contract, operation, arguments, deadline):
        started = time.monotonic()
        trace = {'endpoint': endpoint.name, 'outcome': 'unconfirmed', 'work_budget_seconds': max(0, deadline - started),
                 'work_seconds': None, 'cleanup_seconds': None, 'wall_seconds': None,
                 'protocol_version': None, 'pid': None, 'owned_child_observed': False, 'process_reaped': False, 'tool_call_sent': False,
                 'wire_result_received': False, 'server_info': None, 'server_runtime': None, 'tool_error_code': None}
        work_finished = None
        with tempfile.TemporaryDirectory(prefix='enterprise-chaos-') as tmp:
            record = Path(tmp).absolute() / 'owned.pid'
            done = anyio.Event()
            parameters = StdioServerParameters(command=sys.executable, args=[
                '-c', 'exec(' + repr(self._bootstrap(endpoint)) + ')', str(record),
                'enterprise_ai_lab.mcp_gateway', 'serve', '--database', str(endpoint.database),
                '--audit', str(endpoint.audit), '--profile', contract.profile])

            async def guardian():
                try:
                    with anyio.move_on_after(max(0, deadline - time.monotonic())):
                        await done.wait()
                    if done.is_set():
                        # Normal SDK EOF shutdown gets a small bounded grace;
                        # deadline/cancellation cleanup does not abandon a child.
                        await anyio.sleep(.15)
                finally:
                    _kill_owned(record)

            result, error = None, None
            try:
                with anyio.fail_after(max(0, deadline - time.monotonic())):
                    async with anyio.create_task_group() as group:
                        group.start_soon(guardian)
                        try:
                            async with Client(parameters, mode='auto', read_timeout_seconds=max(.001, deadline - time.monotonic()), cache=None) as client:
                                try:
                                    pid = _owned(record)
                                    if pid is None:
                                        raise ReadFailure('cleanup_unconfirmed')
                                    trace.update(pid=pid, owned_child_observed=True, protocol_version=client.protocol_version)
                                    try:
                                        info = client.server_info.model_dump(mode='json', by_alias=True, exclude_none=True)
                                        runtime = json.loads(client.instructions.rsplit('\nRuntime fingerprint: ', 1)[1])
                                        if (info['name'] != 'enterprise-legacy-fixture'
                                                or set(runtime) != {'python', 'sqlite', 'mcp', 'mcp_types'}
                                                or runtime['mcp'] != '2.3.0' or runtime['mcp_types'] != '2.3.0'):
                                            raise ValueError()
                                    except Exception:
                                        raise ReadFailure('invalid_catalog') from None
                                    trace.update(server_info=info, server_runtime=runtime)
                                    catalog = await client.list_tools()
                                    if (catalog.next_cursor is not None
                                            or [tool.name for tool in catalog.tools] != contract.expected_catalog()
                                            or any(tool.annotations is None or tool.annotations.read_only_hint is not True
                                                   or tool.annotations.destructive_hint is not False for tool in catalog.tools)):
                                        raise ReadFailure('invalid_catalog')
                                    await self._before_call(endpoint, client, record)
                                    trace['tool_call_sent'] = True
                                    result = await client.call_tool(operation, arguments,
                                        read_timeout_seconds=max(.001, deadline - time.monotonic()))
                                    trace['wire_result_received'] = True
                                    if result.is_error:
                                        try:
                                            code = json.loads(result.content[0].text)['error']['code']
                                            trace['tool_error_code'] = code if code in ERRORS else None
                                        except Exception:
                                            pass
                                finally:
                                    work_finished = time.monotonic()
                                    done.set()
                        finally:
                            done.set()
                            group.cancel_scope.cancel()
            except TimeoutError:
                error = 'transport_timeout'
            except ReadFailure as exc:
                error = exc.code
            except Exception as exc:
                if isinstance(exc, BaseExceptionGroup):
                    def failures(value):
                        if isinstance(value, BaseExceptionGroup):
                            return [v for child in value.exceptions for v in failures(child)]
                        return [value]
                    leaves = failures(exc)
                    codes = [v.code for v in leaves if isinstance(v, ReadFailure)]
                    error = codes[0] if len(codes) == len(leaves) and len(set(codes)) == 1 else None
                error = error or _transport_code(exc) or 'internal_error'
            except BaseException as exc:
                trace['outcome'] = ('caller_cancelled' if isinstance(exc, anyio.get_cancelled_exc_class()) else 'interrupted')
                raise
            finally:
                # Native caller cancellation propagates, never reaching reader's
                # transport failover branch. Synchronous ownership checks are bounded.
                _kill_owned(record)
                pid = _pid(record)
                if trace['pid'] is None:
                    trace['pid'] = pid
                if pid is not None:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        trace['process_reaped'] = True
                ended = time.monotonic()
                work_finished = work_finished or ended
                trace.update(work_seconds=round(work_finished - started, 6),
                             cleanup_seconds=round(ended - work_finished, 6), wall_seconds=round(ended - started, 6))
                self.records.append(trace)
            if result is not None and error in (None, 'transport_timeout', 'transport_closed'):
                if result.is_error is True:
                    error = 'tool_error'
                elif error is not None:
                    # Timeout/closure while disposing an already received result
                    # cannot turn a tool/payload failure into another tool call.
                    error = 'attempt_deadline'
            if not trace['process_reaped']:
                error = 'cleanup_unconfirmed'
            if time.monotonic() >= deadline:
                if error == 'transport_closed':
                    error = 'transport_timeout'
                elif error is None and result is not None and result.is_error is not True:
                    # A received response (including malformed data) is not an
                    # unconfirmed transport failure. Cleanup cannot create a retry.
                    error = 'attempt_deadline'
            trace['outcome'] = error or ('tool_error' if result.is_error else 'response')
            if error is not None:
                raise AttemptFailure(error, trace)
            return result, trace
