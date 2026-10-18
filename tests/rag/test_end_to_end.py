"""Opt-in real TCP/issuer/ONNX/Qdrant integration; no retrieval or issuer doubles.

Enable RUN_RAG_INTEGRATION=1 and provide RAG_MODEL_DIR, IDENTITY_LAB_DIR and
optionally NODE_BINARY. Enabled tests fail if their explicit local setup is absent.
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from enterprise_ai_lab.rag.integration import IdentityFixture, RagServer, RagSession, citation_content


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'src' / 'enterprise_ai_lab' / 'rag' / 'data' / 'documents.jsonl'


@unittest.skipUnless(os.environ.get('RUN_RAG_INTEGRATION') == '1',
                     'Real model/process integration requires explicit RUN_RAG_INTEGRATION=1.')
class RealRagIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.model_dir = Path(os.environ['RAG_MODEL_DIR'])
        cls.identity_dir = Path(os.environ['IDENTITY_LAB_DIR'])
        cls.node = os.environ.get('NODE_BINARY')
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.session = RagSession(cls.identity_dir, cls.model_dir, Path(cls.temporary.name) / 'vectors',
                                 fixture=FIXTURE, node=cls.node)
        cls.addClassCleanup(cls.session.close)

    def call(self, client='reader-alpha', *, token_options=None, **options):
        token = self.session.identity.token(client, **(token_options or {})) if client else None
        return self.session.server.query(token, **options)

    def test_reader_and_analyst_have_distinct_retrieval_contexts(self):
        reader_status, reader = self.call(query='What is the document processing limit?')
        analyst_status, analyst = self.call('analyst-alpha', query='What is the reserve buffer?')
        self.assertEqual(reader_status, 200)
        self.assertEqual(analyst_status, 200)
        self.assertTrue(reader['human_review_required'])
        self.assertTrue(analyst['human_review_required'])
        self.assertIn('20 documents', reader['answer'])
        self.assertIn('18 percent', analyst['answer'])
        self.assertNotIn('ALPHA_INTERNAL_RESERVE_18', json.dumps(reader))
        self.assertNotIn('BETA_INTERNAL_RESERVE_42', json.dumps(reader))
        self.assertNotIn('BETA_INTERNAL_RESERVE_42', json.dumps(analyst))
        self.assertNotEqual(reader['citations'], analyst['citations'])
        self.assertEqual({row['document_id'] for row in reader['citations']}, {'alpha-onboarding'})
        self.assertEqual({row['document_id'] for row in analyst['citations']}, {'alpha-reserve'})
        for row in reader['citations']:
            self.assertEqual(row['tenant_id'], 'alpha')
            self.assertIn('reader', row['allowed_roles'])
            self.assertIn(row['quote'], row['text'])
        for row in analyst['citations']:
            self.assertEqual(row['tenant_id'], 'alpha')
            self.assertIn('analyst', row['allowed_roles'])
            self.assertIn(row['quote'], row['text'])

    def test_cross_tenant_request_denied_before_retrieval(self):
        for client, tenant in [('reader-alpha', 'beta'), ('analyst-alpha', 'beta'), ('reader-beta', 'alpha')]:
            with self.subTest(client=client, tenant=tenant):
                status, body = self.call(client, tenant=tenant)
                self.assertEqual(status, 403)
                self.assertEqual(set(body), {'error'})

    def test_observer_role_denied_despite_real_valid_scope(self):
        status, body = self.call('observer-alpha')
        self.assertEqual(status, 403)
        self.assertEqual(set(body), {'error'})

    def test_no_bearer_or_changed_signature_cannot_establish_identity(self):
        self.assertEqual(self.call(None)[0], 401)
        token = self.session.identity.token()
        header, payload, signature = token.split('.')
        forged = header + '.' + payload + '.' + ('A' if signature[0] != 'A' else 'B') + signature[1:]
        status, body = self.session.server.query(forged)
        self.assertEqual(status, 401)
        self.assertEqual(set(body), {'error'})

    def test_reports_audiences_and_opaque_tokens_cannot_be_reused_for_rag(self):
        for resource in ['urn:identity-boundary:reports:jwt', 'urn:identity-boundary:reports:opaque']:
            with self.subTest(resource=resource):
                status, body = self.call(token_options={'resource': resource})
                self.assertEqual(status, 401)
                self.assertEqual(set(body), {'error'})

    def test_missing_or_wrong_resource_scope_denied(self):
        for scope in ['', 'reports:read']:
            with self.subTest(scope=scope):
                status, body = self.call(token_options={'scope': scope})
                self.assertEqual(status, 403)
                self.assertEqual(set(body), {'error'})

    def test_body_cannot_select_a_different_tenant_role_or_filter(self):
        for extra in [{'tenant_id': 'beta'}, {'roles': ['analyst']}, {'filter': {'tenant_id': 'beta'}}]:
            with self.subTest(field=next(iter(extra))):
                status, body = self.call(body={'query': 'What is the reserve buffer?', 'top_k': 3, **extra})
                self.assertEqual(status, 422)
                self.assertEqual(set(body), {'error'})

    def test_identity_headers_and_duplicate_bearers_rejected(self):
        for headers in [{'x-tenant-id': 'beta'}, {'x-roles': 'analyst'}, {'x-role': 'analyst'}, {'x-user-id': 'analyst-alpha'}]:
            with self.subTest(field=next(iter(headers))):
                self.assertEqual(self.call(headers=headers)[0], 400)
        token = self.session.identity.token()
        response = self.session.server._client.post(
            self.session.server.url + '/v1/tenants/alpha/rag',
            headers=[('authorization', 'Bearer ' + token), ('authorization', 'Bearer ' + token)],
            json={'query': 'What is the document processing limit?', 'top_k': 1})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(set(response.json()), {'error'})

    def test_query_and_top_k_types_are_enforced_over_real_http(self):
        for body in [{'query': ' ', 'top_k': 1}, {'query': 5, 'top_k': 1}, {'query': 'limit', 'top_k': True},
                     {'query': 'limit', 'top_k': 0}, {'query': 'limit', 'top_k': 6},
                     {'query': 'limit', 'top_k': '1'}, {'query': 'x' * 2001, 'top_k': 1}]:
            with self.subTest(query_type=type(body['query']).__name__, top_k=body['top_k']):
                self.assertEqual(self.call(body=body)[0], 422)

    def test_embedding_token_limit_rejects_input_without_tainting_the_index(self):
        # Each repeated word becomes at least one embedding token. This stays
        # inside the HTTP character bound and crosses the pinned model's cap.
        query = 'a ' * 300
        self.assertLessEqual(len(query), 2000)
        status, body = self.call(query=query)
        self.assertEqual(status, 422)
        self.assertEqual(set(body), {'error'})
        status, restored = self.call(query='What is the document processing limit?')
        self.assertEqual(status, 200)
        self.assertIn('20 documents', restored['answer'])
        self.assertEqual({row['document_id'] for row in restored['citations']}, {'alpha-onboarding'})

    def test_another_tenant_has_its_own_vector_answer(self):
        status, body = self.call('reader-beta', tenant='beta', query='What is the document processing limit?')
        self.assertEqual(status, 200)
        self.assertIn('75 documents', body['answer'])
        self.assertNotIn('20 documents', body['answer'])
        self.assertNotIn('ALPHA_INTERNAL_RESERVE_18', json.dumps(body))
        self.assertNotIn('BETA_INTERNAL_RESERVE_42', json.dumps(body))
        self.assertEqual({row['document_id'] for row in body['citations']}, {'beta-onboarding'})

    def test_top_k_one_keeps_the_best_permitted_evidence(self):
        status, body = self.call(query='What is the document processing limit?', top_k=1)
        self.assertEqual(status, 200)
        self.assertEqual(len(body['citations']), 1)
        self.assertEqual(body['citations'][0]['document_id'], 'alpha-onboarding')
        self.assertIn('reader', body['citations'][0]['allowed_roles'])

    def test_role_filtered_private_question_and_unanswerable_question_abstain(self):
        for client, query in [('reader-alpha', 'What is the reserve buffer?'),
                              ('analyst-alpha', 'What is the Neptune launch password?')]:
            with self.subTest(client=client, query=query):
                status, body = self.call(client, query=query)
                self.assertEqual(status, 200)
                self.assertTrue(body['abstained'])
                self.assertEqual(body['citations'], [])
                self.assertEqual(body['evidence'], [])
                self.assertNotIn('ALPHA_INTERNAL_RESERVE_18', json.dumps(body))
                self.assertNotIn('BETA_INTERNAL_RESERVE_42', json.dumps(body))

    def test_real_jwt_expiry_is_checked_after_resource_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            with IdentityFixture(self.identity_dir, node=self.node, ttl_seconds=1) as identity:
                with RagServer(identity.issuer, self.model_dir, Path(directory) / 'vectors', seed_fixture=FIXTURE) as server:
                    token = identity.token()
                    time.sleep(1.1)
                    status, body = server.query(token)
                    self.assertEqual(status, 401)
                    self.assertEqual(set(body), {'error'})

    def test_issuer_outage_uses_previously_pinned_public_keys_without_live_introspection(self):
        with tempfile.TemporaryDirectory() as directory:
            with RagSession(self.identity_dir, self.model_dir, Path(directory) / 'vectors',
                            fixture=FIXTURE, node=self.node) as session:
                token = session.identity.token()
                first_status, first = session.server.query(token)
                self.assertEqual(first_status, 200)
                session.identity.stop_issuer()
                status, body = session.server.query(token)
                self.assertEqual(status, 200)
                self.assertEqual(body['citations'], first['citations'])
                self.assertEqual(body['answer'], first['answer'])

    def test_vectors_survive_actual_resource_process_restart_without_seed_replay(self):
        status, before = self.call(query='What is the document processing limit?')
        self.assertEqual(status, 200)
        old_pid = self.session.server._process._child.pid
        self.session.restart()
        self.assertNotEqual(self.session.server._process._child.pid, old_pid)
        status, after = self.call(query='What is the document processing limit?')
        self.assertEqual(status, 200)
        self.assertEqual(citation_content(after['citations']), citation_content(before['citations']))
        for left, right in zip(after['citations'], before['citations']):
            self.assertAlmostEqual(left['score'], right['score'], delta=1e-6)
        self.assertEqual(after['answer'], before['answer'])


if __name__ == '__main__':
    unittest.main()
