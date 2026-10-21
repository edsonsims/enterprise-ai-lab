"""Fixed, labelled test-only faults around actual gateway reads and durable audits."""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sqlite3
import time

import anyio

from ..mcp_gateway.audit import GatewayAudit
from ..mcp_gateway.store import initialize_fixture
from .client import FailoverReader
from .contract import Budget, LocalEndpoint, ReadFailure
from .transport import StdioDriver, _BOOTSTRAP


class _FaultDriver(StdioDriver):
    """Experiment-only scheduling/process faults; never selectable by read CLI."""
    def __init__(self, faults=None, *, marker=None):
        super().__init__()
        self.faults = faults or {}
        self.marker = marker
        self.locks = []

    def _bootstrap(self, endpoint):
        fault = self.faults.get(endpoint.name, 'none')
        if fault not in ('kill_after_read', 'disconnect_before_read', 'stall_after_read', 'kill_after_intent', 'wrong_tenant'):
            return _BOOTSTRAP
        injection = '''
from enterprise_ai_lab.mcp_gateway.service import GatewayService
from enterprise_ai_lab.mcp_gateway.store import ReadOnlyStore
import signal,time
from pathlib import Path
fault = FAULT
marker = MARKER
original = GatewayService.call
def controlled(self, *args, **kwargs):
    if fault == 'disconnect_before_read':
        os._exit(86)
    result = original(self, *args, **kwargs)
    if marker:
        Path(marker).write_text('real read and durable outcome completed before test-only fault')
    if fault == 'kill_after_read':
        os.kill(os.getpid(), signal.SIGKILL)
    if fault == 'stall_after_read':
        time.sleep(5)
    if fault == 'wrong_tenant':
        result['meta']['tenant_id'] = 'beta'
    return result
GatewayService.call = controlled
if fault == 'kill_after_intent':
    original_query = ReadOnlyStore._query
    def killed_query(self, sql, parameters):
        os.kill(os.getpid(), signal.SIGKILL)
        return original_query(self, sql, parameters)
    ReadOnlyStore._query = killed_query
'''.replace('FAULT', repr(fault)).replace('MARKER', repr(str(self.marker) if self.marker else None))
        return _BOOTSTRAP.replace("sys.argv=sys.argv[2:]", injection + "\nsys.argv=sys.argv[2:]")

    async def _before_call(self, endpoint, client, record):
        fault = self.faults.get(endpoint.name)
        if fault == 'source_locked':
            lock = sqlite3.connect(endpoint.database, timeout=.1)
            lock.execute('BEGIN EXCLUSIVE')
            self.locks.append(lock)
        elif fault == 'audit_locked':
            GatewayAudit(endpoint.audit).events()
            lock = sqlite3.connect(endpoint.audit, timeout=.1)
            lock.execute('BEGIN IMMEDIATE')
            self.locks.append(lock)

    async def attempt(self, *args, **kwargs):
        try:
            return await super().attempt(*args, **kwargs)
        finally:
            for lock in self.locks:
                lock.rollback()
                lock.close()
            self.locks.clear()


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _audit(endpoint):
    return GatewayAudit(endpoint.audit).events()


async def run_demo(work_dir):
    work_dir = Path(work_dir).absolute()
    work_dir.mkdir(parents=True, exist_ok=False)
    endpoints = []
    for name in ('primary', 'secondary'):
        source = work_dir / (name + '.sqlite')
        initialize_fixture(source)
        endpoints.append(LocalEndpoint(name, source, work_dir / (name + '-audit.sqlite')))
    original_hashes = {endpoint.name: _sha(endpoint.database) for endpoint in endpoints}
    observations, all_traces = [], []

    async def case(name, faults=None, *, operation='get_work_order', arguments=None, budget=Budget(), expected=None):
        driver = _FaultDriver(faults)
        before = {endpoint.name: len(_audit(endpoint)) for endpoint in endpoints}
        admission_start = time.monotonic()
        reader = FailoverReader(endpoints, 'alpha-reader', budget=budget, _driver=driver)
        admission_seconds = time.monotonic() - admission_start
        started = time.monotonic()
        result, error = None, None
        try:
            result = await reader.read(operation, arguments if arguments is not None else {'work_order_id': 'WO-0001'})
        except ReadFailure as exc:
            error = exc.code
        elapsed = time.monotonic() - started
        assert error == expected, (name, error, expected)
        traces = driver.records
        assert 1 <= len(traces) <= 2 and all(v['process_reaped'] and v['owned_child_observed'] for v in traces), name
        added = {endpoint.name: _audit(endpoint)[before[endpoint.name]:] for endpoint in endpoints}
        if expected is None:
            assert result['response']['meta']['tenant_id'] == 'alpha'
        observations.append({'case': name, 'kind': 'actual_official_mcp_stdio',
            'test_only_injections': faults or {}, 'operation': operation,
            'arguments': arguments if arguments is not None else {'work_order_id': 'WO-0001'},
            'budget': {'total_work_seconds': budget.total_seconds, 'per_attempt_work_seconds': budget.attempt_seconds,
                       'maximum_attempts': budget.max_attempts}, 'admission_seconds': round(admission_seconds, 6),
            'whole_read_seconds': round(elapsed, 6), 'outcome': error or 'success',
            'response': result['response'] if result else None, 'attempts': traces, 'audit_events_added': added})
        all_traces.extend(traces)
        return observations[-1]

    healthy = await case('healthy_primary')
    killed = await case('primary_killed_after_durable_outcome', {'primary': 'kill_after_read'})
    assert len(killed['attempts']) == 2 and all(len(v) == 2 for v in killed['audit_events_added'].values())
    await case('primary_disconnect_before_read', {'primary': 'disconnect_before_read'})
    pending = await case('primary_killed_after_intent', {'primary': 'kill_after_intent'})
    assert [event['phase'] for event in pending['audit_events_added']['primary']] == ['intent']
    stalled = await case('primary_stalled_after_read', {'primary': 'stall_after_read'}, budget=Budget(4, 1))
    assert len(stalled['attempts']) == 2 and stalled['attempts'][0]['outcome'] == 'transport_timeout'
    await case('both_transports_disconnect', {'primary': 'disconnect_before_read', 'secondary': 'disconnect_before_read'}, expected='attempts_exhausted')
    deadline = await case('total_work_deadline', {'primary': 'stall_after_read'}, budget=Budget(1, 3), expected='total_deadline')
    assert len(deadline['attempts']) == 1 and not deadline['audit_events_added']['secondary']
    for name, operation, arguments, fault in (
        ('terminal_permission_denial', 'get_asset_diagnostics', {'asset_id': 'AS-001'}, None),
        ('terminal_hidden_row', 'get_work_order', {'work_order_id': 'WO-0002'}, None),
        ('terminal_invalid_tenant_argument', 'list_work_orders', {'tenant_id': 'beta'}, None),
        ('terminal_source_busy', 'get_work_order', {'work_order_id': 'WO-0001'}, 'source_locked'),
        ('terminal_audit_unavailable', 'get_work_order', {'work_order_id': 'WO-0001'}, 'audit_locked'),
    ):
        row = await case(name, {'primary': fault} if fault else None, operation=operation, arguments=arguments, expected='tool_error')
        assert len(row['attempts']) == 1 and not row['audit_events_added']['secondary']
    invalid = await case('terminal_wire_provenance_mismatch', {'primary': 'wrong_tenant'}, expected='provenance_mismatch')
    assert len(invalid['attempts']) == 1 and not invalid['audit_events_added']['secondary']
    recovery = await case('explicit_fresh_primary_recovery')
    assert recovery['response'] == healthy['response'] and len(recovery['attempts']) == 1

    # Explicit caller cancellation after an independently visible durable outcome.
    marker = work_dir / 'test-only-completed-read.marker'
    driver = _FaultDriver({'primary': 'stall_after_read'}, marker=marker)
    reader = FailoverReader(endpoints, 'alpha-reader', budget=Budget(5, 3), _driver=driver)
    before = {endpoint.name: len(_audit(endpoint)) for endpoint in endpoints}
    scopes, results = [], []
    done = anyio.Event()
    started = time.monotonic()
    async def cancelled_read():
        with anyio.CancelScope() as scope:
            scopes.append(scope)
            results.append(await reader.read('get_work_order', {'work_order_id': 'WO-0001'}))
        done.set()
    async with anyio.create_task_group() as group:
        group.start_soon(cancelled_read)
        with anyio.fail_after(3):
            while not marker.exists():
                await anyio.sleep(.005)
        scopes[0].cancel()
        with anyio.fail_after(2):
            await done.wait()
    assert not results and scopes[0].cancelled_caught
    assert len(driver.records) == 1 and driver.records[0]['process_reaped']
    added = {endpoint.name: _audit(endpoint)[before[endpoint.name]:] for endpoint in endpoints}
    assert len(added['primary']) == 2 and not added['secondary']
    observations.append({'case': 'caller_cancellation_after_durable_outcome', 'kind': 'actual_official_mcp_stdio',
        'test_only_injections': {'primary': 'stall_after_read', 'parent': 'explicit_anyio_cancellation_after_child_marker'},
        'operation': 'get_work_order', 'arguments': {'work_order_id': 'WO-0001'},
        'budget': {'total_work_seconds': 5, 'per_attempt_work_seconds': 3, 'maximum_attempts': 2},
        'outcome': 'caller_cancelled', 'response': None, 'attempts': driver.records, 'audit_events_added': added,
        'whole_read_seconds': round(time.monotonic() - started, 6), 'cancellation_did_not_rollback_read': True})
    all_traces.extend(driver.records)
    assert all(_sha(endpoint.database) == original_hashes[endpoint.name] for endpoint in endpoints)
    audits = {endpoint.name: _audit(endpoint) for endpoint in endpoints}
    own = Path(__file__).parent
    gateway = own.parent / 'mcp_gateway'
    return {'schema_version': 1, 'study': 'fixture-bound-local-read-failover-v1',
        'recorded_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        'data_kind': 'authored_synthetic', 'identity_kind': 'trusted_local_launcher_profile_not_authentication',
        'profile': 'alpha-reader', 'human_review_performed': False,
        'runtime': {'python': platform.python_version(), 'platform': platform.platform(), 'sqlite': sqlite3.sqlite_version,
                    'mcp': version('mcp'), 'mcp_types': version('mcp-types')},
        'source_sha256': {str(path.relative_to(own.parent)): _sha(path) for path in sorted([*own.glob('*.py'), *gateway.glob('*.py'), gateway / 'data/work-orders-v1.json'])},
        'requirements_mcp_lock_sha256': _sha(Path(__file__).parents[3] / 'requirements-mcp.lock') if (Path(__file__).parents[3] / 'requirements-mcp.lock').exists() else None,
        'source_databases_before': original_hashes,
        'source_databases_after': {endpoint.name: _sha(endpoint.database) for endpoint in endpoints},
        'source_unchanged': True, 'observations': observations, 'actual_processes': len(all_traces),
        'all_observed_children_reaped': all(v['process_reaped'] and v['owned_child_observed'] for v in all_traces),
        'audits': audits, 'audit_chains_verified': True,
        'observed_degradation': {'sample_size_each': 1, 'healthy_whole_read_seconds': healthy['whole_read_seconds'],
            'kill_failover_whole_read_seconds': killed['whole_read_seconds'],
            'stall_failover_whole_read_seconds': stalled['whole_read_seconds'],
            'kill_minus_healthy_seconds': round(killed['whole_read_seconds'] - healthy['whole_read_seconds'], 6),
            'interpretation': 'Single controlled local observations including process start/handshake/cleanup; no performance or availability guarantee'},
        'limits': ['Only fixed local named reads on this immutable synthetic dataset; not a generic production retrier',
                   'Work/acceptance deadlines are absolute; cleanup is separately observed and SDK-shielded, not hard real-time wall exit',
                   'At most one failover; any tool error, payload/provenance failure or caller cancellation is terminal',
                   'Timeout/closure may follow a read and durable outcome; repeated reads can produce duplicate audit attempts',
                   'No exactly-once, atomic audit/source transaction, aborted-SQL or rollback claim',
                   'Host/filesystem and launcher are trusted; no remote authentication, model, Oracle/SAP, action or write tool']}
