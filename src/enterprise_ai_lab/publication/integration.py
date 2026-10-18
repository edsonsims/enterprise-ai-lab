"""Actual public PDF/OCR → durable approval → permission-filtered RAG evidence.

The approver is a scripted OAuth client, not a person. No network downloads occur.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time

import httpx
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from ..approval.bootstrap import verify_cli
from ..approval.contracts import format_utc
from ..rag.auth import APPROVAL_AUDIENCE, local_origin
from ..rag.integration import IdentityFixture, IntegrationError, _JsonProcess
from ..rag.models import LocalEmbedder, verify_model
from ..rag.pdf_corpus import load_pdf_documents
from ..rag.pdf_demo import extraction_runtime
from ..rag.store import VectorStore
from .gateway import workflow_id
from .storage import CandidateRepository
from .workflow import DocumentPublicationWorkflow


class LocalProcess:
    def __init__(self, command, *, issuer, address, queue, database, model_dir=None, store=None, pause=0):
        root = Path(__file__).resolve().parents[3]
        argv = [sys.executable, "-m", "enterprise_ai_lab.publication", command,
            "--issuer", issuer, "--address", address, "--task-queue", queue,
            "--database", str(database), "--port", "0"]
        if command == "resource":
            argv += ["--model-dir", str(model_dir), "--store", str(store), "--pause-after-commit", str(pause)]
        self.process = _JsonProcess(argv, cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src"),
            "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1"})
        self.client = httpx.Client(timeout=30, trust_env=False, follow_redirects=False)
        try:
            ready = self.process.read()
            if ready.get("event") != "ready":
                raise IntegrationError("Publication process did not become ready.")
            self.url = local_origin(ready["url"])
            if self.client.get(self.url + "/healthz").status_code != 200:
                raise IntegrationError("Publication process did not become healthy.")
        except BaseException:
            self.close(); raise

    def call(self, token, *, tenant="alpha", request_id=None, decision=False, method="POST", body=None):
        path = f"/v1/tenants/{tenant}/publications"
        if request_id is not None:
            path += "/" + request_id
        if decision:
            path += "/decisions"
        response = self.client.request(method, self.url + path,
            headers={"authorization": "Bearer " + token} if token else {}, json=body if method == "POST" else None)
        result = response.json()
        if not isinstance(result, dict) or (token and token in json.dumps(result)):
            raise IntegrationError("Publication response was invalid or reflected a credential.")
        return response.status_code, result

    def query(self, token, tenant, question):
        response = self.client.post(self.url + f"/v1/tenants/{tenant}/rag",
            headers={"authorization": "Bearer " + token}, json={"query": question, "top_k": 5})
        return response.status_code, response.json()

    def kill(self):
        self.process._child.kill(); self.process._child.wait(timeout=3); self.close()

    def close(self):
        self.client.close(); self.process.close()


async def run_demo(*, temporal_cli: Path, identity_dir: Path, node: str, model_dir: Path,
                   manifest_path: Path, work_dir: Path, out: Path) -> dict:
    cli, embedding = verify_cli(temporal_cli), verify_model(model_dir)
    work_dir, out = Path(work_dir).resolve(), Path(out).resolve()
    identity_dir, model_dir = Path(identity_dir).resolve(), Path(model_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    if any(work_dir.iterdir()):
        raise ValueError("choose a fresh work directory; durable state is preserved")
    documents = load_pdf_documents(Path(manifest_path).resolve())  # actual native/OCR, no indexing
    groups = {tenant: [doc for doc in documents if doc.tenant_id == tenant] for tenant in ("alpha", "beta")}
    if any(len(batch) != 2 or {d.provenance.method for d in batch} != {"native", "ocr"} for batch in groups.values()):
        raise ValueError("this bounded demo requires the documented two-page public fixture")
    database, store_path = work_dir / "publication.sqlite3", work_dir / "vectors"
    candidates = CandidateRepository(database)
    staged = {tenant: candidates.stage(batch) for tenant, batch in groups.items()}
    # A new policy version narrows only Alpha's native page to analysts. The
    # unchanged OCR page remains analyst-only. Both exact grants are reviewed.
    narrowed = candidates.stage([replace(doc, allowed_roles=("analyst",)) for doc in groups["alpha"]])
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    address, queue = f"127.0.0.1:{port}", "publication-demo-v1"
    server_db = work_dir / "temporal.sqlite3"
    identity = IdentityFixture(identity_dir, node=node)
    env = resource = edge = None
    observations, histories = [], []
    native_question = "How is access to individual enterprise resources granted?"
    ocr_question = "What functions is the Core composed of?"
    pages = {doc.document_id: doc for doc in documents}

    def require(condition, message):
        if not condition:
            raise IntegrationError(message)

    async def host():
        return await WorkflowEnvironment.start_local(port=port, ip="127.0.0.1", identity="publication-study-recorder",
            dev_server_existing_path=str(Path(temporal_cli).resolve()),
            dev_server_database_filename=str(server_db), ui=False, dev_server_log_level="error")

    def start_resource(pause=0):
        return LocalProcess("resource", issuer=identity.issuer, address=address, queue=queue,
            database=database, model_dir=model_dir, store=store_path, pause=pause)

    def start_edge():
        return LocalProcess("serve", issuer=identity.issuer, address=address, queue=queue, database=database)

    def token(client="requester-alpha", *, resource=APPROVAL_AUDIENCE, scope=None):
        return identity.token(client, resource=resource, scope=scope)

    def observe(case, *, client="requester-alpha", expected=200, token_value=None, **kwargs):
        code, body = edge.call(token_value if token_value is not None else token(client), **kwargs)
        require(code == expected, f"Publication case {case} returned unexpected HTTP status {code}.")
        observations.append({"case": case, "http_status": code, "response": body})
        return body

    def query(case, *, client="reader-alpha", tenant="alpha", question=native_question, expected=200,
              visible=None, method=None):
        code, body = resource.query(token(client, resource="urn:identity-boundary:rag"), tenant, question)
        require(code == expected, f"RAG case {case} returned unexpected status {code}.")
        if code == 200:
            if visible is False:
                require(body["abstained"] and not body["citations"], "Unpublished or forbidden content reached retrieval.")
            if visible is True:
                require(not body["abstained"] and bool(body["citations"]), "Approved content was not retrieved.")
            roles = {"reader", "analyst"} if client.startswith("analyst") else {"reader"}
            for citation in body["citations"]:
                source = pages[citation["document_id"]]
                span = citation["provenance"]
                require(citation["tenant_id"] == tenant and bool(roles.intersection(citation["allowed_roles"])),
                    "A citation crossed tenant or role policy.")
                require(source.text[span["start"]:span["end"]] == citation["text"] and citation["quote"] in citation["text"],
                    "A literal citation did not resolve to its canonical page span.")
                original = source.provenance.as_dict()
                for key in ("source_id", "source_uri", "source_sha256", "source_page", "artifact_sha256",
                            "artifact_page", "method", "page_text_sha256"):
                    require(span[key] == original[key], "Publication changed original or artifact provenance.")
                if method:
                    require(span["method"] == method, "Unexpected extraction path.")
        observations.append({"case": case, "http_status": code, "query": question,
            "client": client, "tenant": tenant, "response": body})
        return body

    def request(request_id, *, tenant="alpha", revision=0, candidate=None, lifetime=300):
        return {"request_id": request_id,
            "candidate_sha256": (candidate or staged[tenant]).candidate_sha256,
            "expected_revision": revision,
            "expires_at": format_utc(datetime.now(timezone.utc) + timedelta(seconds=lifetime))}

    def decision(snapshot, command="review-1", verdict="approve"):
        return {"command_id": command, "snapshot_sha256": snapshot["request"]["snapshot_sha256"], "decision": verdict}

    async def settled(request_id, status, publication, *, tenant="alpha", timeout=65):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, body = edge.call(token("requester-" + tenant), tenant=tenant, request_id=request_id, method="GET")
            if code == 200 and body["status"] == status and body["publication_status"] == publication:
                return body
            require(code in (200, 503), "Publication status returned unexpected failure.")
            await asyncio.sleep(0.1)
        raise IntegrationError("Publication workflow did not settle within the bounded deadline.")

    primary = request("publish-public-pages")
    closed = []
    try:
        env = await host(); resource = start_resource(); edge = start_edge()
        query("staged-not-searchable", visible=False)
        no = observe("reject-created", body=request("reject-pages"))
        observe("rejected", client="approver-alpha", request_id="reject-pages", decision=True, body=decision(no, verdict="reject"))
        await settled("reject-pages", "rejected", "not_applicable")
        closed.append(("alpha", "reject-pages"))
        query("rejected-not-searchable", visible=False)
        exp = observe("expiry-created", body=request("expire-pages", lifetime=4))
        resource.close(); resource = None
        await asyncio.sleep(4.2)
        resource = start_resource()
        expired = await settled("expire-pages", "expired", "not_applicable")
        observations.append({"case": "expired-while-worker-absent", "http_status": 200, "response": expired})
        observe("expired-approval-denied", client="approver-alpha", request_id="expire-pages", decision=True,
            body=decision(exp), expected=409)
        closed.append(("alpha", "expire-pages"))
        query("expired-not-searchable", visible=False)

        created = observe("publication-created", body=primary)
        observe("exact-create-replay", body=primary)
        observe("changed-revision-conflict", body={**primary, "expected_revision": 1}, expected=409)
        observe("foreign-candidate-denied", body=request("foreign-candidate", candidate=staged["beta"]), expected=404)
        observe("candidate-missing", body={**request("missing-candidate"), "candidate_sha256": "0" * 64}, expected=404)
        review = decision(created)
        good = token("approver-alpha"); header, payload, signature = good.split(".")
        forged = header + "." + payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        for name, credential, code in [
            ("forged-approval-denied", forged, 401),
            ("rag-audience-denied", token("analyst-alpha", resource="urn:identity-boundary:rag"), 401),
            ("cross-tenant-approval-denied", token("approver-beta"), 403),
            ("wrong-role-denied", token("reader-alpha"), 403),
            ("missing-scope-denied", token("approver-alpha", scope=""), 403)]:
            observe(name, token_value=credential, request_id=primary["request_id"], decision=True, body=review, expected=code)
        observe("tampered-snapshot-denied", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**review, "snapshot_sha256": "0" * 64}, expected=409)
        observe("spoofed-actor-denied", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**review, "actor_id": "forged"}, expected=422)
        query("pending-not-searchable", visible=False)

        # Inspect actual Qdrant inventory with its owner closed. Empty answers
        # alone would not prove that pending candidates were absent from index.
        edge.close(); edge = None; resource.close(); resource = None
        store = VectorStore(store_path, LocalEmbedder(model_dir))
        try:
            unpublished_vector_count = store.client.count(store.collection_name, exact=True).count
            require(unpublished_vector_count == 0, "Pending/rejected/expired pages entered the vector index.")
        finally:
            store.close()
        await env.shutdown(); env = await host()
        resource = start_resource(pause=15); edge = start_edge()
        restored = observe("pending-after-all-services-restart", request_id=primary["request_id"], method="GET")
        require(restored["request"] == created["request"], "Restart changed the approved snapshot.")
        query("pending-after-restart-not-searchable", visible=False)
        observe("approval-accepted", client="approver-alpha", request_id=primary["request_id"], decision=True, body=review)
        deadline = time.monotonic() + 12
        while not candidates.list_effects() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        effects = candidates.list_effects()
        require(len(effects) == 1, "No committed publication observed before crash.")
        before_crash = await env.client.get_workflow_handle(workflow_id("alpha", primary["request_id"])).fetch_history(rpc_timeout=timedelta(seconds=3))
        require(not any(e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED for e in before_crash.events),
            "Crash probe missed the after-publication/before-ack window.")
        resource.kill(); resource = None; edge.close(); edge = None
        await env.shutdown(); env = await host()
        resource = start_resource(); edge = start_edge()
        published = await settled(primary["request_id"], "approved", "published")
        observations.append({"case": "published-once-after-kill-and-server-restart", "http_status": 200, "response": published})
        closed.append(("alpha", primary["request_id"]))
        require(len(candidates.list_effects()) == 1, "Activity retry duplicated publication.")
        query("approved-native-reader", visible=True, method="native")
        query("approved-ocr-analyst", client="analyst-alpha", question=ocr_question, visible=True, method="ocr")
        query("ocr-reader-denied", question=ocr_question, visible=False)
        query("beta-still-unpublished", client="reader-beta", tenant="beta", question=ocr_question, visible=False)
        query("cross-tenant-query-denied", client="analyst-alpha", tenant="beta", expected=403)
        observe("changed-decision-conflict", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**review, "decision": "reject"}, expected=409)

        # Identical public source pages have distinct signed tenant grants.
        beta = observe("beta-created", client="requester-beta", tenant="beta", body=request(primary["request_id"], tenant="beta"))
        observe("beta-approved", client="approver-beta", tenant="beta", request_id=primary["request_id"], decision=True, body=decision(beta))
        await settled(primary["request_id"], "approved", "published", tenant="beta")
        closed.append(("beta", primary["request_id"]))
        query("beta-ocr-reader", client="reader-beta", tenant="beta", question=ocr_question, visible=True, method="ocr")

        narrow = observe("acl-revision-created", body=request("narrow-native-role", revision=1, candidate=narrowed))
        query("pending-acl-keeps-approved-version", visible=True, method="native")
        observe("acl-revision-approved", client="approver-alpha", request_id="narrow-native-role", decision=True, body=decision(narrow))
        await settled("narrow-native-role", "approved", "published")
        closed.append(("alpha", "narrow-native-role"))
        query("approved-acl-removes-reader", visible=False)
        query("approved-acl-preserves-analyst", client="analyst-alpha", visible=True, method="native")
        observe("old-decision-replay", client="approver-alpha", request_id=primary["request_id"], decision=True, body=review)
        query("old-replay-cannot-restore-reader", visible=False)

        # A separately accepted old revision must fail CAS, not replace live ACLs.
        stale = observe("stale-revision-created", body=request("stale-publication", revision=0))
        observe("stale-revision-approval", client="approver-alpha", request_id="stale-publication", decision=True, body=decision(stale))
        failed = await settled("stale-publication", "approved", "failed")
        observations.append({"case": "stale-revision-publication-failed", "http_status": 200, "response": failed})
        closed.append(("alpha", "stale-publication"))
        query("stale-approval-cannot-restore-reader", visible=False)
        before_restart = query("ocr-before-resource-restart", client="analyst-alpha", question=ocr_question, visible=True, method="ocr")
        resource.close(); resource = start_resource()
        after_restart = query("ocr-after-resource-restart", client="analyst-alpha", question=ocr_question, visible=True, method="ocr")
        require(before_restart["citations"] == after_restart["citations"], "Resource restart changed citations or source provenance.")
        query("narrowed-acl-after-restart", visible=False)
        effects = candidates.list_effects()
        require(len(effects) == 3, "Rejected, expired, stale or repeated requests changed publication ledger.")

        out.parent.mkdir(parents=True, exist_ok=True)
        replayer = Replayer(workflows=[DocumentPublicationWorkflow])
        for tenant, request_id in closed:
            handle = env.client.get_workflow_handle(workflow_id(tenant, request_id))
            # The CAS conflict intentionally closes as a failed workflow; its
            # queryable snapshot and failure history remain part of the report.
            from temporalio.client import WorkflowFailureError
            try:
                await handle.result(rpc_timeout=timedelta(seconds=5))
            except WorkflowFailureError:
                require(request_id == "stale-publication", "Unexpected workflow failure.")
            history = await handle.fetch_history(rpc_timeout=timedelta(seconds=3))
            await replayer.replay_workflow(history)
            filename = f"histories/{tenant}-{request_id}.json"
            path = out.parent / filename; path.parent.mkdir(parents=True, exist_ok=True)
            raw = (history.to_json() + "\n").encode(); path.write_bytes(raw)
            histories.append({"tenant_id": tenant, "request_id": request_id, "filename": filename,
                "sha256": hashlib.sha256(raw).hexdigest(), "event_count": len(history.events),
                "event_types": [EventType.Name(event.event_type) for event in history.events]})
        require(candidates.list_effects() == effects, "Workflow replay changed publication ledger.")
        resource.close(); resource = None
        final_store = VectorStore(store_path, LocalEmbedder(model_dir))
        try:
            published_vector_count = final_store.client.count(final_store.collection_name, exact=True).count
        finally:
            final_store.close()
        result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "experiment": "Public PDF/OCR staged immutably, actual durable approval, permission-filtered published vectors",
            "runtime": {**extraction_runtime(), "temporalio": version("temporalio"), "cli": cli,
                "onnxruntime": version("onnxruntime"), "qdrant-client": version("qdrant-client"),
                "node": subprocess.run([node, "--version"], check=True, capture_output=True,
                    timeout=5).stdout.decode().strip(),
                "identity_bridge_sha256": hashlib.sha256((identity_dir / "dist/bridge.js").read_bytes()).hexdigest()},
            "embedding": embedding, "candidates": [asdict(value) for value in [*staged.values(), narrowed]],
            "observations": observations, "effects": effects, "histories": histories,
            "index": {"before_any_approval": unpublished_vector_count, "after_publications": published_vector_count},
            "durability": {"temporal_resource_edge_restarted_while_pending": True,
                "identity_issuer_kept_running": True, "worker_killed_after_commit": True,
                "before_crash_activity_completed": False, "temporal_server_restarted": True,
                "primary_effect_count": 1, "rejected_expired_stale_effect_count": 0,
                "old_approval_cannot_restore_acl": True, "citations_identical_after_resource_restart": True},
            "replay": {"histories": len(histories), "all_passed": True, "no_new_publications": True},
            "human_review_performed": False,
            "limitations": ["Reviewer is a scripted client-credentials service fixture, not a person's approval.",
                "The connected publication resource enforces the gate; trusted standalone rag ingest remains a separate operator tool.",
                "Local staging, Temporal gRPC and filesystem administration are trusted; no hostile-upload sandbox or production IAM.",
                "Only two English public NIST pages and authored tenant grants; no OCR/answer-quality benchmark.",
                "Extractive answers preserve literal evidence, not semantic entailment; no new neural-model evaluation.",
                "Partial vector writes remain fail-closed and require fresh-store rebuild; preserving the ledger is required for deduplication."]}
        encoded = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        out.write_text(encoded)
        return json.loads(encoded)  # report and return value share JSON's list representation
    finally:
        if edge is not None: edge.close()
        if resource is not None: resource.close()
        if env is not None: await env.shutdown()
        identity.close()
