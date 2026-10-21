"""Real file/SQLite admission; no subprocess, network, auth or model double claim."""
import os
import sqlite3

import pytest

from enterprise_ai_lab.chaos.contract import Budget, FixtureContract, LocalEndpoint, ReadFailure
from enterprise_ai_lab.mcp_gateway.store import initialize_fixture


@pytest.fixture
def replicas(tmp_path):
    endpoints = []
    for name in ('primary', 'secondary'):
        source = tmp_path / (name + '.sqlite')
        initialize_fixture(source)
        endpoints.append(LocalEndpoint(name, source, tmp_path / (name + '-audit.sqlite')))
    return endpoints


@pytest.mark.parametrize('kwargs', [{'total_seconds': True}, {'total_seconds': float('nan')}, {'total_seconds': float('inf')}, {'total_seconds': 0}, {'attempt_seconds': -1}, {'max_attempts': True}, {'max_attempts': 3}, {'max_attempts': 0}])
def test_invalid_budget_rejected(kwargs):
    with pytest.raises(ValueError):
        Budget(**kwargs)


def test_one_or_two_fixed_endpoints_only(replicas):
    FixtureContract(replicas[:1], 'alpha-reader')
    with pytest.raises(ValueError):
        FixtureContract([], 'alpha-reader')
    with pytest.raises(ValueError):
        FixtureContract(replicas[::-1], 'alpha-reader')
    with pytest.raises(ValueError):
        FixtureContract([*replicas, replicas[0]], 'alpha-reader')


@pytest.mark.parametrize('table,statement', [
    ('work_orders', "UPDATE work_orders SET summary='intentional fictional drift' WHERE tenant_id='alpha' AND work_order_id='WO-0001'"),
    ('work_order_roles', "UPDATE work_order_roles SET role='reader' WHERE tenant_id='alpha' AND work_order_id='WO-0002'"),
])
def test_matching_provenance_metadata_is_insufficient_when_content_or_acl_differs(replicas, table, statement):
    with sqlite3.connect(replicas[1].database) as connection:
        connection.execute(statement)
    with pytest.raises(ReadFailure) as error:
        FixtureContract(replicas, 'alpha-reader')
    assert error.value.code == 'provenance_mismatch'
    assert not any(endpoint.audit.exists() for endpoint in replicas)


@pytest.mark.parametrize('suffix', ['', '-wal', '-shm', '-journal'])
def test_cross_endpoint_audit_family_cannot_alias_source(replicas, suffix):
    os.link(replicas[0].database, str(replicas[1].audit) + suffix)
    with pytest.raises(ReadFailure) as error:
        FixtureContract(replicas, 'alpha-reader')
    assert error.value.code == 'endpoint_alias'


def test_late_hardlink_refused_before_another_attempt(replicas):
    contract = FixtureContract(replicas, 'alpha-reader')
    os.link(replicas[0].database, str(replicas[1].audit) + '-shm')
    with pytest.raises(ReadFailure) as error:
        contract.before_attempt(replicas[0])
    assert error.value.code == 'endpoint_alias'


def test_same_profile_is_fixed_for_all_endpoints(replicas):
    contract = FixtureContract(replicas, 'alpha-reader')
    assert contract.profile == 'alpha-reader'
    assert contract.principal.tenant_id == 'alpha'
    assert contract.expected_catalog() == ['list_work_orders', 'get_work_order']
    assert all(not hasattr(endpoint, 'profile') for endpoint in replicas)


def test_source_replacement_or_drift_is_terminal(replicas):
    contract = FixtureContract(replicas, 'alpha-reader')
    with sqlite3.connect(replicas[0].database) as connection:
        connection.execute("UPDATE work_orders SET title='changed during test' WHERE tenant_id='alpha' AND work_order_id='WO-0001'")
    with pytest.raises(ReadFailure) as error:
        contract.before_attempt(replicas[0])
    assert error.value.code == 'provenance_mismatch'
