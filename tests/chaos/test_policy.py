"""Controlled-clock policy tests; these are not transport/inference observations."""
import copy
from pathlib import Path
import sqlite3

import anyio
from mcp.types import CallToolResult, TextContent
import pytest

from enterprise_ai_lab.chaos.client import AttemptFailure, FailoverReader
from enterprise_ai_lab.chaos.contract import Budget, LocalEndpoint, ReadFailure
from enterprise_ai_lab.mcp_gateway.audit import ERRORS, canonical
from enterprise_ai_lab.mcp_gateway.store import fixture_source, initialize_fixture


@pytest.fixture
def endpoints(tmp_path):
    result = []
    for name in ('primary', 'secondary'):
        source = tmp_path / (name + '.sqlite')
        initialize_fixture(source)
        result.append(LocalEndpoint(name, source, tmp_path / (name + '-audit.sqlite')))
    return result


def response():
    row = fixture_source()[0]['work_orders'][0]
    return {'data': {'item': {k: v for k, v in row.items() if k != 'allowed_roles'}},
            'meta': {'operation': 'get_work_order', 'tenant_id': 'alpha'}}


def wire(body=None, *, error=False, text=None):
    body = response() if body is None else body
    return CallToolResult(content=[TextContent(type='text', text=canonical(body) if text is None else text)],
                          structured_content=None if error else body, is_error=error)


class Clock:
    value = 0.0
    def __call__(self):
        return self.value


class Driver:
    def __init__(self, clock, actions):
        self.clock, self.actions, self.calls = clock, iter(actions), []
    async def attempt(self, endpoint, contract, operation, arguments, deadline):
        self.calls.append({'endpoint': endpoint.name, 'deadline': deadline, 'arguments': copy.deepcopy(arguments)})
        outcome, work, cleanup = next(self.actions)
        self.clock.value += work + cleanup
        trace = {'endpoint': endpoint.name, 'work_seconds': work, 'cleanup_seconds': cleanup}
        if type(outcome) is str:
            raise AttemptFailure(outcome, trace)
        return outcome, trace


def run(endpoints, actions, *, budget=Budget(), args=None):
    clock = Clock()
    driver = Driver(clock, actions)
    reader = FailoverReader(endpoints, 'alpha-reader', budget=budget, _driver=driver, _clock=clock)
    try:
        return anyio.run(reader.read, 'get_work_order', args or {'work_order_id': 'WO-0001'}), driver
    except Exception as exc:
        exc.driver = driver
        raise


def test_exact_remaining_total_includes_previous_cleanup(endpoints):
    result, driver = run(endpoints, [('transport_closed', .6, .2), (wire(), .1, 0)], budget=Budget(1, .6))
    assert result['endpoint'] == 'secondary'
    assert [v['deadline'] for v in driver.calls] == [.6, 1.0]
    assert result['elapsed_seconds'] == .9


def test_cleanup_consumes_total_without_starting_secondary(endpoints):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [('transport_timeout', .6, .5), (wire(), 0, 0)], budget=Budget(1, .6))
    assert error.value.code == 'total_deadline'
    assert len(error.value.driver.calls) == 1
    assert len(error.value.attempts) == 1


@pytest.mark.parametrize('budget', [Budget(3, 2, 1), Budget(3, 2, 2)])
def test_maximum_attempts_is_explicit(endpoints, budget):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [('transport_closed', .1, 0)] * 3, budget=budget)
    assert error.value.code == 'attempts_exhausted'
    assert len(error.value.driver.calls) == budget.max_attempts


@pytest.mark.parametrize('work,cleanup,code', [(1.1, 0, 'total_deadline'), (.1, .95, 'total_deadline'), (.7, 0, 'attempt_deadline')])
def test_late_success_is_never_accepted_or_failed_over(endpoints, work, cleanup, code):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(wire(), work, cleanup), (wire(), 0, 0)], budget=Budget(1, .6))
    assert error.value.code == code
    assert len(error.value.driver.calls) == 1


@pytest.mark.parametrize('work,cleanup', [(1.1, 0), (.1, 1.0)])
def test_late_secondary_response_is_terminal_after_primary_timeout(
    endpoints, work, cleanup,
):
    """Controlled work/cleanup clock, not an actual MCP timing observation."""
    unused_third = (wire(), 0, 0)
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [
            ('transport_timeout', .6, .2),
            (wire(), work, cleanup),
            unused_third,
        ], budget=Budget(4, 1, 2))
    assert error.value.code == 'attempt_deadline'
    driver = error.value.driver
    assert [call['endpoint'] for call in driver.calls] == ['primary', 'secondary']
    assert [call['deadline'] for call in driver.calls] == [1, 1.8]
    assert len(error.value.attempts) == len(driver.calls) == 2
    assert driver.clock.value < 4  # Its own attempt budget, not the total, expired.
    assert next(driver.actions) is unused_third


@pytest.mark.parametrize('category', ['invalid_catalog', 'invalid_payload', 'provenance_mismatch', 'internal_error', 'cleanup_unconfirmed', 'audit_unavailable'])
def test_non_transport_attempt_failures_are_terminal(endpoints, category):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(category, .1, 0), (wire(), 0, 0)])
    assert error.value.code == category
    assert len(error.value.driver.calls) == 1


@pytest.mark.parametrize('code', ERRORS)
def test_every_gateway_tool_error_is_terminal(endpoints, code):
    result = wire({'error': {'code': code, 'message': 'bounded test response'}}, error=True)
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(result, .1, 0), (wire(), 0, 0)])
    assert error.value.code == 'tool_error'
    assert len(error.value.driver.calls) == 1
    assert len(error.value.attempts) == 1


def test_received_tool_error_cannot_turn_into_retry_during_slow_cleanup(endpoints):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(wire({'error': {'code': 'audit_unavailable'}}, error=True), .1, .6), (wire(), 0, 0)], budget=Budget(2, .5))
    assert len(error.value.driver.calls) == 1


@pytest.mark.parametrize('mutation', ['foreign_tenant', 'foreign_row', 'extra_field', 'unreadable_row'])
def test_success_payload_cannot_bypass_fixture_grants(endpoints, mutation):
    body = response()
    if mutation == 'foreign_tenant':
        body['meta']['tenant_id'] = 'beta'
    elif mutation == 'foreign_row':
        body['data']['item']['tenant_id'] = 'beta'
    elif mutation == 'extra_field':
        body['meta']['secret'] = 'unexpected'
    else:
        hidden = fixture_source()[0]['work_orders'][1]
        body['data']['item'] = {k: v for k, v in hidden.items() if k != 'allowed_roles'}
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(wire(body), .1, 0), (wire(), 0, 0)])
    assert error.value.code in ('invalid_payload', 'provenance_mismatch')
    assert len(error.value.driver.calls) == 1


@pytest.mark.parametrize('text', ['{}', '{"data":1,"data":2}', '{"data":NaN}'])
def test_inconsistent_duplicate_or_nonfinite_json_is_terminal(endpoints, text):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(wire(text=text), .1, 0), (wire(), 0, 0)])
    assert error.value.code == 'invalid_payload'
    assert len(error.value.driver.calls) == 1


def test_invalid_request_success_is_refused_even_if_schema_valid_rows_match(endpoints):
    with pytest.raises(ReadFailure) as error:
        run(endpoints, [(wire(), .1, 0), (wire(), 0, 0)], args={'work_order_id': 'WO-0001', 'tenant_id': 'alpha'})
    assert error.value.code == 'provenance_mismatch'
    assert len(error.value.driver.calls) == 1


def test_mutating_callers_arguments_during_await_does_not_change_snapshot(endpoints):
    args = {'work_order_id': 'WO-0001'}
    class MutatingDriver:
        async def attempt(self, endpoint, contract, operation, arguments, deadline):
            args['work_order_id'] = 'WO-0002'
            assert arguments == {'work_order_id': 'WO-0001'}
            return wire(), {'endpoint': endpoint.name}
    reader = FailoverReader(endpoints, 'alpha-reader', _driver=MutatingDriver())
    result = anyio.run(reader.read, 'get_work_order', args)
    assert result['response']['data']['item']['work_order_id'] == 'WO-0001'


def test_caller_cancellation_propagates_without_second_attempt(endpoints):
    calls = []
    async def check():
        entered = anyio.Event()
        class CancelDriver:
            async def attempt(self, endpoint, *args):
                calls.append(endpoint.name)
                entered.set()
                await anyio.sleep_forever()
        reader = FailoverReader(endpoints, 'alpha-reader', _driver=CancelDriver())
        scopes, results = [], []
        async def task():
            with anyio.CancelScope() as scope:
                scopes.append(scope)
                results.append(await reader.read('get_work_order', {'work_order_id': 'WO-0001'}))
        async with anyio.create_task_group() as group:
            group.start_soon(task)
            await entered.wait()
            scopes[0].cancel()
        assert scopes[0].cancelled_caught and not results
    anyio.run(check)
    assert calls == ['primary']


def test_replica_drift_after_first_transport_failure_prevents_fallback(endpoints):
    clock = Clock()
    class DriftingDriver(Driver):
        async def attempt(self, *args):
            with sqlite3.connect(endpoints[1].database) as connection:
                connection.execute("UPDATE work_orders SET title='intentional test drift' WHERE tenant_id='alpha' AND work_order_id='WO-0001'")
            return await super().attempt(*args)
    driver = DriftingDriver(clock, [('transport_closed', .1, 0), (wire(), 0, 0)])
    reader = FailoverReader(endpoints, 'alpha-reader', _driver=driver, _clock=clock)
    with pytest.raises(ReadFailure) as error:
        anyio.run(reader.read, 'get_work_order', {'work_order_id': 'WO-0001'})
    assert error.value.code == 'provenance_mismatch'
    assert len(driver.calls) == len(error.value.attempts) == 1
