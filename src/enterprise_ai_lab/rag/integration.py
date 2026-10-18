"""Real-process OAuth → authenticated vector RAG orchestration.

The issuer protocol is private child stdio. It carries ephemeral bearer tokens;
raw protocol messages, process environments and HTTP request headers are never
included in reports or exceptions. No model is downloaded by this module.
"""
from __future__ import annotations

import json
import hashlib
import os
import queue
import shutil
import subprocess
import sys
import threading
from contextlib import AbstractContextManager
from importlib.metadata import version
from pathlib import Path

import httpx


class IntegrationError(RuntimeError):
    """A local process or integration assertion failed without exposing credentials."""


class _JsonProcess:
    def __init__(self, command: list[str], *, cwd: Path, env: dict[str, str], timeout=120):
        self.timeout = timeout
        self._messages: queue.Queue = queue.Queue(maxsize=32)
        self._stderr = bytearray()
        self._child = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._closed = False
        self._stdout_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def _read_stdout(self):
        try:
            while True:
                line = self._child.stdout.readline(65537)
                if not line:
                    self._messages.put_nowait(None)
                    return
                if len(line) > 65536 or not line.endswith(b'\n'):
                    raise ValueError('Invalid child protocol line.')
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError('Invalid child protocol object.')
                self._messages.put_nowait(value)
        except (ValueError, UnicodeError, queue.Full, OSError):
            self._child.terminate()
            try:
                self._messages.put_nowait(None)
            except queue.Full:
                pass

    def _read_stderr(self):
        # Drain stderr to avoid deadlocks. Keep only a bounded private prefix;
        # child text can never become a report or an exception message.
        try:
            while True:
                chunk = self._child.stderr.read(4096)
                if not chunk:
                    return
                remaining = 65536 - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])
        except (OSError, ValueError):
            return

    def read(self) -> dict:
        try:
            value = self._messages.get(timeout=self.timeout)
        except queue.Empty:
            raise IntegrationError('A local child did not produce a bounded protocol response.') from None
        if value is None:
            raise IntegrationError('A local child exited before its expected protocol response.')
        return value

    def write(self, value: dict):
        data = (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
        if len(data) > 8193:
            raise IntegrationError('The private fixture command exceeds its byte limit.')
        try:
            self._child.stdin.write(data)
            self._child.stdin.flush()
        except (BrokenPipeError, OSError):
            raise IntegrationError('The private fixture command could not be delivered.') from None

    def close(self, *, eof=False):
        if self._closed:
            return
        self._closed = True
        try:
            self._child.stdin.close()
            if not eof and self._child.poll() is None:
                self._child.terminate()
            try:
                self._child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self._child.kill()
                self._child.wait(timeout=2)
        finally:
            self._child.stdout.close()
            self._child.stderr.close()
            self._stdout_thread.join(timeout=1)
            self._stderr_thread.join(timeout=1)
            self._stderr.clear()
            while True:
                try:
                    self._messages.get_nowait()
                except queue.Empty:
                    break


class IdentityFixture(AbstractContextManager):
    """Actual oidc-provider child. Tokens stay in this caller's memory only."""
    def __init__(self, identity_dir: str | Path, *, node: str | Path | None = None, ttl_seconds=60):
        directory = Path(identity_dir).resolve()
        binary = str(node) if node else shutil.which('node')
        if not binary or not (directory / 'dist' / 'bridge.js').is_file():
            raise IntegrationError('Provide Node and a built Identity Boundary Lab checkout.')
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 60:
            raise IntegrationError('Choose a fixture token lifetime from 1 to 60 seconds.')
        env = {**os.environ, 'BRIDGE_TTL_SECONDS': str(ttl_seconds)}
        self._process = _JsonProcess([binary, str(directory / 'dist' / 'bridge.js')], cwd=directory, env=env)
        self._sequence = 0
        try:
            ready = self._process.read()
            if (ready.get('event') != 'ready' or ready.get('protocol') != 1
                    or ready.get('audience') != 'urn:identity-boundary:rag' or ready.get('scope') != 'rag:query'
                    or not isinstance(ready.get('issuer'), str)):
                raise IntegrationError('The identity fixture did not return the expected protocol.')
            from .auth import local_origin
            self.issuer = local_origin(ready['issuer'])
        except BaseException:
            self.close()
            raise

    def _command(self, operation: str, **values):
        self._sequence += 1
        self._process.write({'id': self._sequence, 'op': operation, **values})
        response = self._process.read()
        if response.get('id') != self._sequence or response.get('ok') is not True:
            raise IntegrationError('The identity fixture could not complete its private command.')
        return response

    def token(self, client_id='reader-alpha', *, resource=None, scope=None):
        options = {}
        if resource is not None:
            options['resource'] = resource
        if scope is not None:
            options['scope'] = scope
        value = self._command('issue', clientId=client_id, **options).get('token')
        if (not isinstance(value, dict) or value.get('token_type') != 'Bearer'
                or not isinstance(value.get('access_token'), str) or not 1 <= len(value['access_token']) <= 4096):
            raise IntegrationError('The identity fixture did not issue a bounded bearer token.')
        return value['access_token']

    def revoke(self, token: str, client_id='reader-alpha'):
        return self._command('revoke', clientId=client_id, token=token)['result']

    def stop_issuer(self):
        self._command('stopIssuer')

    def close(self):
        self._process.close(eof=True)

    def __exit__(self, *_exc):
        self.close()


class RagServer(AbstractContextManager):
    """A real loopback Python resource process and file-backed Qdrant store."""
    def __init__(self, issuer: str, model_dir: str | Path, store_path: str | Path, *,
                 seed_fixture: str | Path | None = None, generator='extractive', generator_model_dir=None):
        directory = Path(__file__).resolve().parents[3]
        command = [sys.executable, '-m', 'enterprise_ai_lab.rag', 'serve', '--issuer', issuer,
                   '--model-dir', str(Path(model_dir).resolve()), '--store', str(Path(store_path).resolve()), '--port', '0',
                   '--generator', generator]
        if seed_fixture is not None:
            command.extend(['--seed-fixture', str(Path(seed_fixture).resolve())])
        if generator_model_dir is not None:
            command.extend(['--generator-model-dir', str(Path(generator_model_dir).resolve())])
        env = {**os.environ, 'PYTHONPATH': str(directory / 'src'),
               'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1', 'HF_HUB_DISABLE_TELEMETRY': '1'}
        self._process = _JsonProcess(command, cwd=directory, env=env)
        self._client = httpx.Client(trust_env=False, follow_redirects=False, timeout=60)
        try:
            ready = self._process.read()
            if ready.get('event') != 'ready' or not isinstance(ready.get('url'), str):
                raise IntegrationError('The RAG resource did not return a ready URL.')
            from .auth import local_origin
            self.url = local_origin(ready['url'])
            if self._client.get(self.url + '/healthz').status_code != 200:
                raise IntegrationError('The RAG resource health check failed.')
        except BaseException:
            self.close()
            raise

    def query(self, token: str | None, *, tenant='alpha', query='What is the document processing limit?', top_k=3,
              body=None, headers=None):
        values = {'authorization': f'Bearer {token}'} if token is not None else {}
        if headers:
            values.update(headers)
        try:
            response = self._client.post(self.url + f'/v1/tenants/{tenant}/rag',
                json=body if body is not None else {'query': query, 'top_k': top_k}, headers=values)
            result = response.json()
        except (httpx.HTTPError, ValueError):
            raise IntegrationError('The local RAG HTTP request could not complete.') from None
        if not isinstance(result, dict):
            raise IntegrationError('The local RAG HTTP response is not an object.')
        if token is not None and token in json.dumps(result):
            raise IntegrationError('The local resource reflected a bearer token.')
        return response.status_code, result

    def close(self):
        self._client.close()
        self._process.close()

    def __exit__(self, *_exc):
        self.close()


class RagSession(AbstractContextManager):
    """Own both actual processes; retain one issuer across a resource restart."""
    def __init__(self, identity_dir, model_dir, store_path, *, node=None, fixture=None,
                 generator='extractive', generator_model_dir=None):
        self.identity = IdentityFixture(identity_dir, node=node)
        self.server = None
        self._model_dir, self._store_path = model_dir, store_path
        self._options = {'generator': generator, 'generator_model_dir': generator_model_dir}
        try:
            self.server = RagServer(self.identity.issuer, model_dir, store_path, seed_fixture=fixture, **self._options)
        except BaseException:
            self.identity.close()
            raise

    def restart(self):
        self.server.close()
        self.server = RagServer(self.identity.issuer, self._model_dir, self._store_path, **self._options)

    def close(self):
        try:
            if self.server is not None:
                self.server.close()
        finally:
            self.identity.close()

    def __exit__(self, *_exc):
        self.close()


def _require(condition, message):
    if not condition:
        raise IntegrationError(message)


def citation_content(rows):
    """Stable cited content; Qdrant persistence can change low-order score bits."""
    return [{key: value for key, value in row.items() if key != 'score'} for row in rows]


def run_demo(identity_dir, model_dir, store_path, out, generator='extractive', generator_model_dir=None, node=None):
    """Run the real cross-repository demo; persist only sanitized synthetic output."""
    fixture = Path(__file__).resolve().parent / 'data' / 'documents.jsonl'
    from .models import verify_model
    embedding = verify_model(model_dir)
    observations = []
    with RagSession(identity_dir, model_dir, store_path, node=node, fixture=fixture,
                    generator=generator, generator_model_dir=generator_model_dir) as session:
        def observe(name, client='reader-alpha', tenant='alpha', *, expected=200, token_options=None,
                    neural_observation=False, **options):
            token = session.identity.token(client, **(token_options or {})) if client else None
            status, body = session.server.query(token, tenant=tenant, **options)
            preserved_error = (generator == 'qwen' and neural_observation and status == 503
                               and body.get('error', {}).get('code') == 'rag_unavailable')
            _require(status == expected or preserved_error,
                     f'The {name} integration case returned an unexpected HTTP status.')
            observation = {'case': name, 'http_status': status, 'response': body}
            if generator == 'qwen' and neural_observation:
                observation['generation_result'] = 'rejected' if preserved_error else 'accepted'
            observations.append(observation)
            return body

        reader = observe('reader_general_documents', query='What is the document processing limit?', neural_observation=True)
        analyst = observe('analyst_private_documents', client='analyst-alpha', query='What is the reserve buffer?',
                          neural_observation=True)
        observe('cross_tenant_denied', tenant='beta', expected=403)
        observe('observer_role_denied', client='observer-alpha', expected=403)
        observe('no_token_denied', client=None, expected=401)
        observe('reports_audience_denied', expected=401,
                token_options={'resource': 'urn:identity-boundary:reports:jwt'})
        observe('missing_scope_denied', expected=403, token_options={'scope': ''})
        observe('identity_header_spoof_denied', expected=400, headers={'x-tenant-id': 'beta', 'x-roles': 'analyst'})
        observe('identity_body_spoof_denied', expected=422,
                body={'query': 'What is the document processing limit?', 'top_k': 3, 'tenant_id': 'beta', 'roles': ['analyst']})
        reader_private = observe('reader_cannot_retrieve_private_document', query='What is the reserve buffer?')
        unknown = observe('unanswerable_query', query='What is the Neptune launch password?')
        if generator == 'qwen':
            observe('related_why_query_is_not_an_entailment_test', neural_observation=True,
                    query='Why is the document processing limit 20 documents per processing batch?')
        session.restart()
        restored = observe('persistent_store_after_restart', query='What is the document processing limit?',
                           neural_observation=True)

        for value in (reader, analyst, reader_private, unknown, restored):
            _require('error' in value or value.get('human_review_required') is True,
                     'The RAG demo must label synthetic outputs for human review.')
        _require(reader_private.get('abstained') is True and unknown.get('abstained') is True,
                 'The role-filtered or unanswerable fixture question did not abstain.')
        _require(reader_private.get('generation_performed') is False and unknown.get('generation_performed') is False,
                 'An empty authorized context must abstain before invoking any generator.')
        for value in (reader, reader_private, unknown, restored):
            serialized = json.dumps(value)
            _require('ALPHA_INTERNAL_RESERVE_18' not in serialized and 'BETA_INTERNAL_RESERVE_42' not in serialized,
                     'The reader response contains a private fixture marker.')
        _require('BETA_INTERNAL_RESERVE_42' not in json.dumps(analyst),
                 'The alpha analyst response contains a different tenant fixture marker.')
        preserved = None
        score_delta = None
        if 'error' not in reader and 'error' not in restored:
            preserved = (citation_content(reader['citations']) == citation_content(restored.get('citations', []))
                         and reader['answer'] == restored.get('answer'))
            if [row['chunk_id'] for row in reader['citations']] == [row['chunk_id'] for row in restored['citations']]:
                score_delta = max((abs(before['score'] - after['score'])
                                   for before, after in zip(reader['citations'], restored['citations'])), default=0.0)
        if generator == 'extractive':
            _require(reader.get('abstained') is False and analyst.get('abstained') is False,
                     'The authorized fixture questions did not produce an evidence-linked answer.')
            _require(reader.get('citations') and analyst.get('citations')
                     and reader['citations'] != analyst['citations'],
                     'Reader and analyst fixture answers must cite their distinct permitted evidence.')
            _require({row.get('document_id') for row in reader['citations']} == {'alpha-onboarding'}
                     and {row.get('document_id') for row in analyst['citations']} == {'alpha-reserve'},
                     'The authorized fixture questions did not cite the expected permitted documents.')
            _require(preserved, 'The persisted vector answer changed after a resource restart without seed replay.')
            _require(score_delta is not None and score_delta <= 1e-6,
                     'The persisted citation score changed beyond the demo tolerance.')
            _require('20 documents' in reader['answer'] and '18 percent' in analyst['answer'],
                     'The extractive fixture answers do not match their permitted source text.')
        else:
            from .neural import LocalQwenGenerator
            for row in observations:
                if row['http_status'] == 200:
                    body = row['response']
                    _require(body.get('generator') == LocalQwenGenerator.name,
                             'The optional neural mode must preserve its selected generator without fallback.')
                    roles = ('reader', 'analyst') if row['case'] == 'analyst_private_documents' else ('reader',)
                    _require(all(hit.get('tenant_id') == 'alpha'
                                 and set(hit.get('allowed_roles', [])).intersection(roles)
                                 for hit in body.get('citations', [])),
                             'The optional neural response cited a document outside its tenant or role.')
                    serialized = json.dumps(body)
                    _require('BETA_INTERNAL_RESERVE_42' not in serialized
                             and ('analyst' in roles or 'ALPHA_INTERNAL_RESERVE_18' not in serialized),
                             'The optional neural response contains an unauthorized fixture marker.')

    report = {'experiment': 'actual local OAuth JWT to authenticated persistent vector RAG',
              'data': 'authored synthetic tenant documents; no customer or employer data',
              'transport': 'real loopback HTTP between separate Node issuer and Python resource processes',
              'identity': {'audience': 'urn:identity-boundary:rag', 'scope': 'rag:query',
                           'validation': 'offline RS256/at+jwt using startup discovery and JWKS snapshot',
                           'token_revocation': 'JWT remains usable until expiry; opaque reports demo is separate'},
              'embedding': embedding,
              'corpus_sha256': hashlib.sha256(fixture.read_bytes()).hexdigest(),
              'runtime': {'python': sys.version.split()[0],
                          **{name: version(name) for name in ('fastembed', 'onnxruntime', 'qdrant-client', 'httpx')}},
              'vector_search': {'backend': 'Qdrant embedded local mode', 'distance': 'Cosine',
                                'authorization_filter': 'tenant equality AND any allowed role, before top-k search'},
              'generator': generator,
              'language_model_invoked': False if generator == 'extractive' else (True if any(
                  row['response'].get('generation_performed') is True for row in observations) else None),
              'observations': observations,
              'persistence': {'resource_restarted': True, 'seed_fixture_replayed': False,
                              'answer_and_cited_content_preserved': preserved,
                              'maximum_cosine_score_delta': score_delta, 'score_absolute_tolerance': 1e-6,
                              'before': reader, 'after': restored},
              'limitations': ['synthetic fixture, not a production security review',
                              'retrieval and exact citations do not establish semantic answer correctness',
                              'embedding similarity and configured threshold do not guarantee answerability',
                              'browser sign-in, key rotation and production IAM remain outside this demo']}
    if generator == 'qwen':
        from .neural import MODEL_ID, MODEL_REVISION, MODEL_FILES, RUNTIME
        report['neural_observations'] = {
            'generation_errors': sum(row.get('generation_result') == 'rejected' for row in observations),
            'accepted_responses': sum(row.get('generation_result') == 'accepted' for row in observations),
            'errors_preserved': True, 'no_fallback': True,
            'error_interpretation': 'HTTP 503 rag_unavailable preserves retrieval/generation rejection; the HTTP error does not expose the internal failure stage.',
            'invocation_interpretation': 'A successful generation_performed response establishes model invocation; if every generation is rejected, the report leaves invocation null because the HTTP error hides its failure stage.',
            'model': MODEL_ID, 'revision': MODEL_REVISION, 'license': 'Apache-2.0',
            'files_sha256_verified_at_resource_startup': dict(MODEL_FILES), 'runtime': dict(RUNTIME),
            'accepted_generation_stats': [
                {'case': row['case'], **row['response']['generation']}
                for row in observations if row['http_status'] == 200 and 'generation' in row['response']],
            'persistence_interpretation': 'Neural responses are observations; use the extractive demo to verify exact answer and citation persistence.',
            'related_why_query': 'A related question can receive a literal source quote without that quote answering why; no entailment claim is made.'}
        report['limitations'].append('Neural structured-output failures remain HTTP 503; successful exact citations do not prove that the answer addresses the question.')
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(report, ensure_ascii=False, indent=2) + '\n'
    if target.exists() and target.read_text(encoding='utf-8') != content:
        raise IntegrationError('The demo output exists; choose a new path to preserve the original run.')
    target.write_text(content, encoding='utf-8')
    return report
