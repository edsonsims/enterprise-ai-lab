"""Actual official MCP child processes, synthetic SQLite and controlled local faults."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys

import anyio
import pytest

from enterprise_ai_lab.chaos.experiment import run_demo


@pytest.fixture(scope='module')
def report(tmp_path_factory):
    return anyio.run(run_demo, tmp_path_factory.mktemp('chaos-process') / 'fresh')


def case(report, name):
    return next(v for v in report['observations'] if v['case'] == name)


def test_actual_official_wire_and_all_owned_children_reaped(report):
    assert len(report['observations']) == 15 and report['actual_processes'] == 20
    traces = [v for row in report['observations'] for v in row['attempts']]
    assert len(traces) == 20
    assert all(v['owned_child_observed'] and v['process_reaped'] and v['pid'] > 1 for v in traces)
    assert {v['protocol_version'] for v in traces} == {'2026-07-28'}
    assert report['runtime']['mcp'] == report['runtime']['mcp_types'] == '2.3.0'


def test_real_post_read_kill_leaves_two_distinct_durable_attempts(report):
    row = case(report, 'primary_killed_after_durable_outcome')
    assert row['outcome'] == 'success'
    assert [v['endpoint'] for v in row['attempts']] == ['primary', 'secondary']
    assert row['attempts'][0]['outcome'] == 'transport_closed'
    events = row['audit_events_added']
    assert all([v['phase'] for v in events[name]] == ['intent', 'outcome'] for name in ['primary', 'secondary'])
    assert events['primary'][0]['call_id'] != events['secondary'][0]['call_id']
    assert events['primary'][0]['input_sha256'] == events['secondary'][0]['input_sha256']
    assert events['primary'][1]['output_sha256'] == events['secondary'][1]['output_sha256']


def test_incomplete_intent_is_preserved_without_deletion_or_claimed_rollback(report):
    row = case(report, 'primary_killed_after_intent')
    assert [v['phase'] for v in row['audit_events_added']['primary']] == ['intent']
    unfinished = row['audit_events_added']['primary'][0]['call_id']
    assert [v['phase'] for v in report['audits']['primary'] if v['call_id'] == unfinished] == ['intent']
    assert row['outcome'] == 'success' and len(row['attempts']) == 2


def test_timeout_and_both_fail_are_bounded_without_hidden_third_attempt(report):
    stalled = case(report, 'primary_stalled_after_read')
    assert stalled['attempts'][0]['outcome'] == 'transport_timeout'
    assert len(stalled['attempts']) == 2
    both = case(report, 'both_transports_disconnect')
    assert both['outcome'] == 'attempts_exhausted' and len(both['attempts']) == 2
    deadline = case(report, 'total_work_deadline')
    assert deadline['outcome'] == 'total_deadline' and len(deadline['attempts']) == 1
    assert deadline['audit_events_added']['secondary'] == []


@pytest.mark.parametrize('name,code', [
    ('terminal_permission_denial', 'forbidden'), ('terminal_hidden_row', 'not_found'),
    ('terminal_invalid_tenant_argument', 'invalid_request'), ('terminal_source_busy', 'resource_busy'),
])
def test_actual_tool_errors_never_reach_secondary(report, name, code):
    row = case(report, name)
    assert row['outcome'] == 'tool_error' and len(row['attempts']) == 1
    assert row['audit_events_added']['secondary'] == []
    assert row['audit_events_added']['primary'][-1]['error_code'] == code


def test_actual_audit_fence_cannot_be_escaped_with_failover(report):
    row = case(report, 'terminal_audit_unavailable')
    assert row['outcome'] == 'tool_error' and len(row['attempts']) == 1
    assert row['audit_events_added']['primary'] == row['audit_events_added']['secondary'] == []


def test_actual_schema_valid_wrong_tenant_response_is_terminal(report):
    row = case(report, 'terminal_wire_provenance_mismatch')
    assert row['outcome'] == 'provenance_mismatch' and len(row['attempts']) == 1
    assert row['audit_events_added']['secondary'] == []


def test_actual_caller_cancellation_does_not_rollback_or_start_another_read(report):
    row = case(report, 'caller_cancellation_after_durable_outcome')
    assert row['outcome'] == 'caller_cancelled' and len(row['attempts']) == 1
    assert row['budget'] == {'total_work_seconds': 5, 'per_attempt_work_seconds': 3, 'maximum_attempts': 2}
    assert row['operation'] == 'get_work_order' and row['arguments'] == {'work_order_id': 'WO-0001'}
    assert row['attempts'][0]['outcome'] == 'caller_cancelled'
    assert len(row['audit_events_added']['primary']) == 2
    assert row['audit_events_added']['primary'][-1]['outcome'] == 'success'
    assert row['audit_events_added']['secondary'] == []


def test_fresh_explicit_recovery_preserves_source_and_old_audit(report):
    healthy = case(report, 'healthy_primary')
    recovered = case(report, 'explicit_fresh_primary_recovery')
    assert healthy['response'] == recovered['response']
    assert len(recovered['attempts']) == 1
    assert report['source_unchanged'] and report['source_databases_before'] == report['source_databases_after']
    assert report['audit_chains_verified']
    assert report['observed_degradation']['sample_size_each'] == 1


def test_cli_refuses_existing_report_before_creating_work(tmp_path):
    out = tmp_path / 'existing.json'
    out.write_text('owner content')
    directory = tmp_path / 'must-not-exist'
    result = subprocess.run([sys.executable, '-m', 'enterprise_ai_lab.chaos', 'demo', '--work-dir', str(directory), '--out', str(out)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2 and not directory.exists()
    assert out.read_text() == 'owner content'
