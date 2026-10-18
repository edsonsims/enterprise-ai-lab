"""Verify behavioral demo invariants without requiring bit-identical cosine scores."""
import json
from pathlib import Path
import sys

report = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
rows = {row['case']: row for row in report['observations']}
expected = {'reader_general_documents': 200, 'analyst_private_documents': 200,
            'cross_tenant_denied': 403, 'observer_role_denied': 403, 'no_token_denied': 401,
            'reports_audience_denied': 401, 'missing_scope_denied': 403,
            'identity_header_spoof_denied': 400, 'identity_body_spoof_denied': 422,
            'reader_cannot_retrieve_private_document': 200, 'unanswerable_query': 200,
            'persistent_store_after_restart': 200}
assert set(rows) == set(expected)
assert all(rows[name]['http_status'] == status for name, status in expected.items())
assert report['generator'] == 'extractive' and report['language_model_invoked'] is False
assert report['persistence']['answer_and_cited_content_preserved'] is True
assert report['persistence']['maximum_cosine_score_delta'] <= 1e-6
for name, text, document in [('reader_general_documents', '20 documents', 'alpha-onboarding'),
                             ('analyst_private_documents', '18 percent', 'alpha-reserve')]:
    response = rows[name]['response']
    assert text in response['answer']
    assert {item['document_id'] for item in response['citations']} == {document}
for name in ('reader_cannot_retrieve_private_document', 'unanswerable_query'):
    response = rows[name]['response']
    assert response['abstained'] is True and response['citations'] == []
    assert response['generation_performed'] is False
print('Connected demo permissions, evidence and persistence verified.')
