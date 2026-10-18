"""Immutable contracts and HTTP admission; durability is tested separately.

The gateway probe and in-process Temporal driver below are explicit doubles.
The HTTP tests use real signature verification and an actual loopback server.
"""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import http.client
import json
import socket
import threading
import time
import unittest

import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa

from enterprise_ai_lab.approval.contracts import (
    ApprovalValidationError, DecisionCommand, DecisionReceipt, validate_decision,
)
from enterprise_ai_lab.publication.api import create_app
from enterprise_ai_lab.publication.contracts import (
    ApplyApprovedDocuments, CandidateMetadata, MAX_REVISION, PublicationSnapshot,
    make_request, validate_effect,
)
from enterprise_ai_lab.publication.gateway import (
    PublicationConflict, PublicationGateway, PublicationNotFound, workflow_id,
)
from enterprise_ai_lab.rag.auth import APPROVAL_AUDIENCE, RAG_AUDIENCE, JwtVerifier
from enterprise_ai_lab.rag.contracts import RagPrincipal


class PublicationContractTests(unittest.TestCase):
    def setUp(self):
        self.request = make_request("batch-1", "alpha", "requester-alpha", "a" * 64, 0,
                                    "2026-10-19T12:00:00Z")
        self.command = DecisionCommand("decide-1", "alpha", self.request.snapshot_sha256,
                                       "approve", "approver-alpha", ("approver",))
        self.receipt = DecisionReceipt(self.command, "2026-10-19T11:59:59Z")

    def test_every_authority_content_revision_and_deadline_field_is_bound(self):
        changes = {"request_id": "batch-2", "tenant_id": "beta", "requested_by": "other",
                   "candidate_sha256": "b" * 64, "expected_revision": 1,
                   "expires_at": "2026-10-20T12:00:00Z"}
        self.request.validate()
        for field, value in changes.items():
            with self.subTest(field=field), self.assertRaises(ApprovalValidationError):
                replace(self.request, **{field: value}).validate()

    def test_revision_is_a_bounded_strict_nonnegative_integer(self):
        for revision in (True, False, -1, 0.0, "0", MAX_REVISION + 1):
            with self.subTest(revision=revision), self.assertRaises(ApprovalValidationError):
                make_request("a", "alpha", "requester", "a" * 64, revision,
                             "2026-10-19T12:00:00Z")
        make_request("a", "alpha", "requester", "a" * 64, MAX_REVISION,
                     "2026-10-19T12:00:00Z").validate()

    def test_candidate_hash_requires_canonical_sha256(self):
        for digest in ("A" * 64, "a" * 63, "g" * 64, None, 1):
            with self.subTest(digest=digest), self.assertRaises(ApprovalValidationError):
                make_request("a", "alpha", "requester", digest, 0, "2026-10-19T12:00:00Z")

    def test_sink_accepts_only_matching_independent_timely_approval(self):
        value = ApplyApprovedDocuments(self.request, self.receipt)
        validate_effect(value)
        commands = [replace(self.command, tenant_id="beta"),
                    replace(self.command, snapshot_sha256="b" * 64),
                    replace(self.command, actor_roles=("requester",)),
                    replace(self.command, actor_id="requester-alpha"),
                    replace(self.command, decision="reject")]
        for command in commands:
            with self.subTest(command=command), self.assertRaises(ApprovalValidationError):
                validate_effect(replace(value, receipt=replace(self.receipt, command=command)))
        for accepted_at in ("2026-10-19T12:00:00Z", "2026-10-19T12:00:01Z"):
            with self.subTest(accepted_at=accepted_at), self.assertRaises(ApprovalValidationError):
                validate_effect(replace(value, receipt=replace(self.receipt, accepted_at=accepted_at)))

    def test_accepted_exact_decision_replays_after_deadline_but_no_other_actor(self):
        after = datetime(2026, 10, 20, tzinfo=timezone.utc)
        validate_decision(self.request, self.command, after, self.receipt)
        for mutation in ({"actor_id": "other-approver"}, {"actor_roles": ("approver", "reader")},
                         {"decision": "reject"}, {"command_id": "other-command"}):
            with self.subTest(mutation=mutation), self.assertRaises(ApprovalValidationError):
                validate_decision(self.request, replace(self.command, **mutation), after, self.receipt)

    def test_new_workflow_identity_cannot_alias_note_or_ambiguous_tenant(self):
        from enterprise_ai_lab.approval.gateway import workflow_id as old_workflow_id
        self.assertNotEqual(workflow_id("alpha", "batch"), old_workflow_id("alpha", "batch"))
        self.assertNotEqual(workflow_id("alpha:a", "b"), workflow_id("alpha", "a:b"))


class CandidateProbe:
    def __init__(self):
        self.metadata = CandidateMetadata("alpha", "a" * 64, 1, ("doc-alpha",),
            ({"document_id": "doc-alpha", "title": "Public source", "allowed_roles": ["reader"],
              "provenance": None},))

    def get_candidate(self, tenant_id, candidate_sha256):
        if (tenant_id, candidate_sha256) != (self.metadata.tenant_id, self.metadata.candidate_sha256):
            raise LookupError("private candidate sentinel")
        return self.metadata


class TemporalProbe:
    """Only exercises gateway comparison/identity; it makes no durability claim."""
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.updates = []
        self.receipt_override = None

    def get_workflow_handle(self, _workflow_id):
        return self

    async def query(self, *_args, **_kwargs):
        return self.snapshot

    async def execute_update(self, _method, command, *, id, **_kwargs):
        self.updates.append((id, command))
        receipt = self.receipt_override or DecisionReceipt(command, "2026-10-18T12:00:00Z")
        self.snapshot = replace(self.snapshot, receipt=receipt, status="approved")
        return receipt


class PublicationGatewayBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        deadline = (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.request = make_request("batch", "alpha", "requester-alpha", "a" * 64, 0, deadline)
        self.backend = TemporalProbe(PublicationSnapshot(self.request, "pending", "not_started"))
        self.candidates = CandidateProbe()
        self.gateway = PublicationGateway(self.backend, "publication-v1", self.candidates)
        self.requester = RagPrincipal("alpha", ("requester",), "requester-alpha")
        self.approver = RagPrincipal("alpha", ("approver",), "approver-alpha")

    async def test_exact_create_replays_while_any_altered_immutable_field_conflicts(self):
        result = await self.gateway.submit(self.requester, "batch", "a" * 64, 0, self.request.expires_at)
        self.assertEqual(result["candidate"]["document_ids"], ("doc-alpha",))
        self.assertNotIn("text", result["candidate"]["documents"][0])
        with self.assertRaises(PublicationConflict):
            await self.gateway.submit(self.requester, "batch", "a" * 64, 1, self.request.expires_at)

    async def test_exact_existing_request_replays_after_deadline(self):
        request = make_request("batch", "alpha", "requester-alpha", "a" * 64, 0,
                               "2020-01-01T00:00:00Z")
        self.backend.snapshot = PublicationSnapshot(request, "expired", "not_applicable")
        result = await self.gateway.submit(self.requester, "batch", "a" * 64, 0, request.expires_at)
        self.assertEqual(result["status"], "expired")

    async def test_candidate_is_tenant_scoped_and_corruption_is_not_client_validation(self):
        with self.assertRaises(PublicationNotFound):
            await self.gateway.submit(self.requester, "batch", "b" * 64, 0, self.request.expires_at)
        self.candidates.metadata = replace(self.candidates.metadata, document_count=2)
        with self.assertRaises(RuntimeError):
            await self.gateway.status(self.requester, "batch")

    async def test_temporal_update_id_binds_full_server_derived_actor_roles(self):
        await self.gateway.decide(self.approver, "batch", "decision", self.request.snapshot_sha256, "approve")
        first_id = self.backend.updates[-1][0]
        self.backend.snapshot = replace(self.backend.snapshot, receipt=None)
        other = replace(self.approver, client_id="other-approver", roles=("approver", "reader"))
        await self.gateway.decide(other, "batch", "decision", self.request.snapshot_sha256, "approve")
        self.assertNotEqual(first_id, self.backend.updates[-1][0])

    async def test_a_deduplicated_receipt_for_another_command_is_never_accepted(self):
        wrong = DecisionCommand("same-id", "alpha", self.request.snapshot_sha256, "reject",
                                "approver-alpha", ("approver",))
        self.backend.receipt_override = DecisionReceipt(wrong, "2026-10-18T12:00:00Z")
        with self.assertRaises(PublicationConflict):
            await self.gateway.decide(self.approver, "batch", "same-id", self.request.snapshot_sha256, "approve")

    async def test_self_review_and_changed_accepted_decision_stop_before_update(self):
        dual = replace(self.requester, roles=("requester", "approver"))
        with self.assertRaises(PublicationConflict):
            await self.gateway.decide(dual, "batch", "d", self.request.snapshot_sha256, "approve")
        self.assertEqual(self.backend.updates, [])
        await self.gateway.decide(self.approver, "batch", "d", self.request.snapshot_sha256, "approve")
        before = len(self.backend.updates)
        await self.gateway.decide(self.approver, "batch", "d", self.request.snapshot_sha256, "approve")
        self.assertEqual(len(self.backend.updates), before)
        with self.assertRaises(PublicationConflict):
            await self.gateway.decide(self.approver, "batch", "d", self.request.snapshot_sha256, "reject")


class HttpGatewayProbe:
    def __init__(self):
        self.calls = []
        self.failure = None

    async def record(self, name, *args):
        self.calls.append((name, args))
        if self.failure:
            raise self.failure
        return {"probe": name}

    async def submit(self, *args): return await self.record("submit", *args)
    async def status(self, *args): return await self.record("status", *args)
    async def decide(self, *args): return await self.record("decide", *args)


class PublicationHttpAdmissionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.public = {**jwt.algorithms.RSAAlgorithm.to_jwk(cls.key.public_key(), as_dict=True),
                      "kid": "publication-boundary", "alg": "RS256", "use": "sig"}

    def setUp(self):
        self.issuer = "http://127.0.0.1:8181"
        self.gateway = HttpGatewayProbe()
        verifier = JwtVerifier(self.issuer, {"keys": [self.public]}, audience=APPROVAL_AUDIENCE)
        self.socket = socket.socket()
        self.socket.bind(("127.0.0.1", 0))
        self.port = self.socket.getsockname()[1]
        self.server = uvicorn.Server(uvicorn.Config(create_app(verifier, self.gateway),
            log_level="error", access_log=False, lifespan="off"))
        self.thread = threading.Thread(target=self.server.run, kwargs={"sockets": [self.socket]}, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 3
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertTrue(self.server.started)

    def tearDown(self):
        self.server.should_exit = True
        self.thread.join(timeout=3)
        self.socket.close()
        self.assertFalse(self.thread.is_alive())

    def token(self, *, role="requester", scope="approval:request", tenant="alpha", audience=APPROVAL_AUDIENCE):
        now = int(time.time())
        client = role + "-" + tenant
        claims = {"iss": self.issuer, "aud": audience, "iat": now, "exp": now + 60,
                  "sub": client, "client_id": client, "jti": "publication-boundary",
                  "tenant_id": tenant, "roles": [role], "scope": scope}
        return jwt.encode(claims, self.key, algorithm="RS256",
                          headers={"kid": "publication-boundary", "typ": "at+jwt"})

    def body(self):
        return {"request_id": "batch", "candidate_sha256": "a" * 64, "expected_revision": 0,
                "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")}

    def call(self, body=None, *, method="POST", path="/v1/tenants/alpha/publications", token=None,
             raw=None, extra_headers=(), with_token=True):
        raw = raw if raw is not None else (b"" if method == "GET" else json.dumps(body or self.body()).encode())
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        try:
            connection.putrequest(method, path)
            if method == "POST": connection.putheader("content-type", "application/json")
            if with_token: connection.putheader("authorization", "Bearer " + (token or self.token()))
            for key, value in extra_headers: connection.putheader(key, value)
            connection.putheader("content-length", str(len(raw)))
            connection.endheaders(raw)
            response = connection.getresponse()
            return response.status, response.read().decode(), dict(response.getheaders())
        finally:
            connection.close()

    def test_verified_requester_identity_and_candidate_revision_reach_backend(self):
        status, _body, headers = self.call()
        self.assertEqual(status, 200)
        self.assertEqual(headers.get("cache-control"), "no-store")
        name, args = self.gateway.calls[0]
        self.assertEqual(name, "submit")
        self.assertEqual(args[0], RagPrincipal("alpha", ("requester",), "requester-alpha"))
        self.assertEqual(args[1:4], ("batch", "a" * 64, 0))

    def test_wrong_audience_tenant_scope_or_role_never_reaches_backend(self):
        self.assertEqual(self.call(with_token=False)[0], 401)
        self.assertEqual(self.call(token=self.token(audience=RAG_AUDIENCE))[0], 401)
        for options in ({"tenant": "beta"}, {"role": "reader"}, {"role": "approver"},
                        {"scope": "approval:decide"}, {"scope": ""}):
            with self.subTest(options=options):
                self.assertEqual(self.call(token=self.token(**options))[0], 403)
        self.assertEqual(self.gateway.calls, [])

    def test_identity_acl_source_and_inline_document_fields_cannot_override_staging(self):
        for field in ("tenant_id", "requested_by", "roles", "allowed_roles", "documents", "text",
                      "provenance", "snapshot_sha256", "actor_id", "action", "target"):
            with self.subTest(field=field):
                status, response, _headers = self.call({**self.body(), field: "private-sentinel"})
                self.assertEqual(status, 422)
                self.assertNotIn("private-sentinel", response)
        self.assertEqual(self.gateway.calls, [])

    def test_noncanonical_digest_and_revision_coercion_are_rejected(self):
        for mutation in ({"candidate_sha256": "A" * 64}, {"expected_revision": True},
                         {"expected_revision": "0"}, {"expected_revision": -1},
                         {"expected_revision": MAX_REVISION + 1}):
            with self.subTest(mutation=mutation):
                self.assertEqual(self.call({**self.body(), **mutation})[0], 422)
        self.assertEqual(self.gateway.calls, [])

    def test_decision_requires_verified_approver_and_forbids_supplied_identity(self):
        path = "/v1/tenants/alpha/publications/batch/decisions"
        body = {"command_id": "d", "snapshot_sha256": "b" * 64, "decision": "approve"}
        token = self.token(role="approver", scope="approval:decide")
        self.assertEqual(self.call(body, path=path, token=token)[0], 200)
        self.assertEqual(self.gateway.calls.pop()[1][0], RagPrincipal("alpha", ("approver",), "approver-alpha"))
        for field in ("actor_id", "actor_roles", "tenant_id", "accepted_at"):
            with self.subTest(field=field):
                self.assertEqual(self.call({**body, field: "override"}, path=path, token=token)[0], 422)
        self.assertEqual(self.call(body, path=path, token=self.token(scope="approval:decide"))[0], 403)
        self.assertEqual(self.gateway.calls, [])

    def test_candidate_status_requires_approval_read_scope_and_requester_or_approver(self):
        path = "/v1/tenants/alpha/publications/batch"
        for options in ({"role": "reader", "scope": "approval:read"}, {"scope": "approval:request"},
                        {"tenant": "beta", "scope": "approval:read"}):
            with self.subTest(options=options):
                self.assertEqual(self.call(method="GET", path=path, token=self.token(**options))[0], 403)
        self.assertEqual(self.call(method="GET", path=path, token=self.token(scope="approval:read"))[0], 200)
        self.assertEqual(len(self.gateway.calls), 1)

    def test_ambiguous_json_oversize_and_identity_headers_stop_before_backend(self):
        for raw, status in ((b'{"request_id":"a","request_id":"b"}', 400),
                            (b'{"expected_revision":NaN}', 400), (b" " * 16385, 413)):
            with self.subTest(raw=raw[:40]): self.assertEqual(self.call(raw=raw)[0], status)
        self.assertEqual(self.call(extra_headers=(("x-roles", "approver"),))[0], 400)
        self.assertEqual(self.call(extra_headers=(("Authorization", "Bearer " + self.token()),))[0], 401)
        self.assertEqual(self.call(path="/v1/tenants/alpha/publications?tenant=beta")[0], 400)
        self.assertEqual(self.gateway.calls, [])

    def test_missing_candidate_conflict_and_backend_details_are_sanitized(self):
        for failure, status in ((PublicationNotFound("private-sentinel"), 404),
                                (PublicationConflict("private-sentinel"), 409),
                                (RuntimeError("private-sentinel"), 503)):
            with self.subTest(status=status):
                self.gateway.failure = failure
                received, body, _headers = self.call()
                self.assertEqual(received, status)
                self.assertNotIn("private-sentinel", body)


if __name__ == "__main__":
    unittest.main()
