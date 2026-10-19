"""Controlled real-worker-absence diagnostic; not the cause of the older CI 503.

The first authenticated request deliberately has no publication worker. Temporal
can accept its immutable start while the snapshot query fails under the unchanged
three-second RPC setting. Its actual cancellation/deadline status is retained
without inferring an underlying cause. One identical request recovers after the real worker
has completed a workflow task. No approval or publication is performed.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import secrets
import subprocess
import sys
import time


SOURCE_FILES = (
    "src/enterprise_ai_lab/diagnostics.py",
    "src/enterprise_ai_lab/publication/gateway.py",
    "src/enterprise_ai_lab/publication/workflow.py",
    "src/enterprise_ai_lab/review/__main__.py",
    "src/enterprise_ai_lab/review/api.py",
    "src/enterprise_ai_lab/review/core.py",
    "src/enterprise_ai_lab/review/failure.py",
    "src/enterprise_ai_lab/review/integration.py",
    "src/enterprise_ai_lab/review/diagnostic_probe.py",
    "public-data/pdf-v1/ingestion.json",
)
REQUEST_FIELDS = frozenset({"request_id", "candidate_sha256", "expected_revision", "expires_at"})
PUBLIC_MANIFEST_SHA256 = "ff0fe614370ece259d8971301a038f95231e4ea9369838d0a7a1c37abe88706e"


def verify_public_manifest(path):
    """This exportable probe accepts only the authored public-data grant bytes."""
    fingerprint = hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if fingerprint != PUBLIC_MANIFEST_SHA256:
        raise ValueError("diagnostic export requires the documented public NIST manifest")
    return fingerprint


def request_fingerprint(body):
    """Fingerprint only the exact finite public submission, never arbitrary HTTP."""
    if (type(body) is not dict or set(body) != REQUEST_FIELDS
            or any(type(body[k]) is not str for k in REQUEST_FIELDS - {"expected_revision"})
            or type(body["expected_revision"]) is not int):
        raise ValueError("invalid diagnostic submission fields")
    from ..publication.contracts import make_request
    make_request(body["request_id"], "alpha", "requester-alpha", body["candidate_sha256"],
                 body["expected_revision"], body["expires_at"])
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(raw).hexdigest()


def source_fingerprints(root):
    """Hash fixed source files; never export a working tree or private state."""
    root = Path(root)
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in SOURCE_FILES}


def history_summary(history):
    from temporalio.api.enums.v1 import EventType
    types = [EventType.Name(event.event_type) for event in history.events]
    return {"event_count": len(types), "workflow_start_count": types.count("EVENT_TYPE_WORKFLOW_EXECUTION_STARTED"),
            "workflow_task_completed_count": types.count("EVENT_TYPE_WORKFLOW_TASK_COMPLETED"),
            "activity_scheduled_count": types.count("EVENT_TYPE_ACTIVITY_TASK_SCHEDULED"),
            "event_types": types}


def _commit(root):
    return subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True, timeout=5).stdout.strip()


def _save_report(path, value):
    encoded = (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode()
    if len(encoded) > 64 * 1024:
        raise ValueError("bounded diagnostic report exceeded its limit")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded); stream.flush(); os.fsync(stream.fileno())


async def run_probe(args):
    from temporalio.testing import WorkflowEnvironment
    from ..approval.bootstrap import verify_cli
    from ..approval.contracts import format_utc
    from ..diagnostics import read_diagnostics
    from ..publication.gateway import RPC_WAIT, workflow_id
    from ..publication.integration import LocalProcess
    from ..publication.storage import CandidateRepository
    from ..rag.integration import IdentityFixture, IntegrationError
    from ..rag.models import verify_model
    from ..rag.pdf_corpus import load_pdf_documents
    from ..rag.pdf_demo import extraction_runtime
    from .failure import capture_failure, http_summary, save_failure
    from .integration import BrowserProtocol, HumanIssuer, Portal, free_port

    work, out = Path(args.work_dir).resolve(), Path(args.out).resolve()
    identity_dir, manifest, model_dir = (Path(value).resolve() for value in
        (args.identity_dir, args.manifest, args.model_dir))
    if out.exists():
        raise ValueError("choose a new diagnostic report path")
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()):
        raise ValueError("choose a fresh private work directory")
    os.chmod(work, 0o700)
    root = Path(__file__).resolve().parents[3]
    sources = source_fingerprints(root)
    cli, embedding = verify_cli(Path(args.temporal_cli)), verify_model(model_dir)
    manifest_hash = verify_public_manifest(manifest)
    documents = [d for d in load_pdf_documents(manifest) if d.tenant_id == "alpha"]
    if len(documents) != 2 or {d.provenance.method for d in documents} != {"native", "ocr"}:
        raise ValueError("this controlled probe requires the two documented public Alpha pages")
    candidates = CandidateRepository(work / "publication.sqlite3")
    candidate = candidates.stage(documents)
    account_password = secrets.token_urlsafe(24)
    accounts = work / "accounts.private.json"
    fd = os.open(accounts, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump({"schema_version": 1, "accounts": [
            {"username": "requester-alpha", "password": account_password}]}, stream)
    port, temporal_port = free_port(), free_port()
    origin, address = f"http://127.0.0.1:{port}", f"127.0.0.1:{temporal_port}"
    queue, request_id = "review-diagnostic-v1", "controlled-accepted-without-worker"
    diagnostics_path = work / "portal-diagnostics.jsonl"
    env = identity = human = portal = resource = browser = None

    def require(condition, message):
        if not condition:
            raise IntegrationError(message)

    try:
        env = await WorkflowEnvironment.start_local(port=temporal_port, ip="127.0.0.1",
            identity="review-diagnostic-study", dev_server_existing_path=str(Path(args.temporal_cli).resolve()),
            dev_server_database_filename=str(work / "temporal.sqlite3"), ui=False, dev_server_log_level="error")
        identity = IdentityFixture(identity_dir, node=args.node)
        human = HumanIssuer(identity_dir, args.node, origin + "/auth/callback", accounts)
        portal = Portal(issuer=human.issuer, address=address, queue=queue, database=candidates.database_path,
            audit=work / "review-audit.sqlite3", manifest=manifest, port=port, diagnostics_path=diagnostics_path)
        browser = BrowserProtocol(origin, human.issuer)
        login = browser.login("requester-alpha", account_password)
        require(login["user"]["subject"] == "requester-alpha", "Expected a real signed-in fixture account.")
        body = {"request_id": request_id, "candidate_sha256": candidate.candidate_sha256,
                "expected_revision": 0,
                "expires_at": format_utc(datetime.now(timezone.utc) + timedelta(minutes=10))}
        submitted_hash = request_fingerprint(body)
        started = time.monotonic()
        initial = browser.call("/api/reviews", method="POST", body=body)
        first_elapsed = time.monotonic() - started
        failure = capture_failure(phase="create_initial", http=http_summary(initial),
            diagnostics_path=diagnostics_path, portal=portal.process, resource=None)
        save_failure(out.with_name(out.stem + "-failure.json"), failure)
        require(initial.status_code == 503 and failure["http"]["error_code"] == "unavailable",
                "Controlled no-worker submission did not yield the expected safe 503.")
        diagnostics = read_diagnostics(diagnostics_path)
        require(failure["diagnostics_status"] == "available" and diagnostics == failure["diagnostics"]
            and len(diagnostics) == 1, "Expected one complete safe failure diagnostic.")
        handle = env.client.get_workflow_handle(workflow_id("alpha", request_id))
        before = await handle.describe(rpc_timeout=timedelta(seconds=3))
        before_history = history_summary(await handle.fetch_history(rpc_timeout=timedelta(seconds=3)))
        require(before.status.name == "RUNNING" and before_history["workflow_start_count"] == 1
            and before_history["workflow_task_completed_count"] == 0,
            "Temporal did not independently confirm the accepted unprocessed workflow.")
        require(candidates.list_effects() == [] and candidates.revision("alpha") == 0,
                "Unconfirmed request unexpectedly published documents.")
        diagnostic = diagnostics[0]
        require(diagnostic["operation"] == "review_create" and diagnostic["stage"] == "temporal_query"
            and diagnostic["category"] == "rpc" and diagnostic["exception_type"] == "rpc_error"
            and diagnostic["rpc_status"] in {"DEADLINE_EXCEEDED", "CANCELLED"},
            "Controlled failure did not identify an actual snapshot RPC cancellation or deadline.")

        # Start the genuine existing resource/worker. Its ready line follows the
        # Worker context entry; its healthz is checked by LocalProcess. Poll only
        # committed history readiness, never repeat the create command blindly.
        resource = LocalProcess("resource", issuer=identity.issuer, address=address, queue=queue,
            database=candidates.database_path, model_dir=model_dir, store=work / "vectors")
        ready_deadline = time.monotonic() + 20
        while True:
            ready_history = history_summary(await handle.fetch_history(rpc_timeout=timedelta(seconds=3)))
            if ready_history["workflow_task_completed_count"]:
                break
            require(time.monotonic() < ready_deadline, "Worker never completed its first actual workflow task.")
            await asyncio.sleep(.05)
        require(request_fingerprint(body) == submitted_hash, "Recovery changed immutable submission fields.")
        recovered = browser.call("/api/reviews", method="POST", body=body)
        require(recovered.status_code == 200, "One exact explicit retry did not recover after worker readiness.")
        result = recovered.json()
        require(result["request"]["request_id"] == request_id
            and result["request"]["candidate_sha256"] == candidate.candidate_sha256
            and result["request"]["expires_at"] == body["expires_at"]
            and result["request"]["expected_revision"] == 0
            and result["request"]["requested_by"] == "requester-alpha"
            and result["status"] == "pending" and result["publication_status"] == "not_started"
            and result["receipt"] is None and result["effect"] is None,
            "Recovered snapshot differs from the exact pending immutable request.")
        after = await handle.describe(rpc_timeout=timedelta(seconds=3))
        after_history = history_summary(await handle.fetch_history(rpc_timeout=timedelta(seconds=3)))
        require(after.run_id == before.run_id and after.status.name == "RUNNING"
            and after_history["workflow_start_count"] == 1 and after_history["activity_scheduled_count"] == 0,
            "Explicit retry restarted or published the accepted workflow.")
        require(candidates.list_effects() == [] and candidates.revision("alpha") == 0,
                "Pending review changed publication state.")
        code, query = resource.query(identity.token("reader-alpha"), "alpha",
            "How is access to individual enterprise resources granted?")
        require(code == 200 and query["abstained"] is True and query["citations"] == [],
                "An unapproved candidate became searchable.")
        require(source_fingerprints(root) == sources, "Probe source changed while the experiment was running.")
        report = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "controlled_fault": True, "fault": "publication_worker_absent_during_initial_create",
            "original_ci_failure_cause": "unknown", "human_review_performed": False,
            "source": {"repository_commit": _commit(root), "file_hashes_bind_measured_working_tree": True,
                "files_sha256": sources,
                "identity_repository_commit": _commit(identity_dir),
                "identity_artifacts_sha256": {name: hashlib.sha256((identity_dir / name).read_bytes()).hexdigest()
                    for name in ("dist/bridge.js", "dist/review-bridge.js", "dist/review-issuer.js")},
                "trusted_manifest_sha256": manifest_hash},
            "runtime": {**extraction_runtime(), "machine": platform.machine(),
                **{name: version(name) for name in ("temporalio", "httpx", "onnxruntime", "qdrant-client")},
                "node": subprocess.run([args.node, "--version"], check=True, capture_output=True,
                    text=True, timeout=5).stdout.strip()},
            "cli": cli, "embedding": embedding,
            "oidc": {"flow": "authorization_code", "pkce": "S256", "subject": "requester-alpha",
                "tenant_id": "alpha", "roles": login["user"]["roles"]},
            "submission": {**body, "canonical_sha256": submitted_hash, "total_post_attempts": 2,
                "identical_retry": True, "rpc_timeout_seconds": RPC_WAIT.total_seconds(),
                "api_operation_timeout_seconds": 12},
            "public_candidate": {"document_count": len(documents),
                "documents": [{"document_id": d.document_id, "tenant_id": d.tenant_id,
                    "allowed_roles": list(d.allowed_roles), "provenance": {name: getattr(d.provenance, name)
                        for name in ("source_id", "source_uri", "source_sha256", "source_page", "artifact_sha256",
                            "artifact_page", "method", "page_text_sha256")}} for d in documents]},
            "initial": {"http_status": initial.status_code, "elapsed_seconds": round(first_elapsed, 6),
                "failure": failure, "workflow_status": before.status.name,
                "run_id": before.run_id, "history": before_history,
                "publication_effect_count": 0, "tenant_revision": 0},
            "recovery": {"http_status": recovered.status_code, "status": result["status"],
                "publication_status": result["publication_status"], "same_workflow_run": True,
                "worker_ready_workflow_task_count": ready_history["workflow_task_completed_count"],
                "history": after_history, "publication_effect_count": 0, "tenant_revision": 0,
                "rag": {"http_status": code, "abstained": query["abstained"], "citation_count": 0}},
            "limitations": ["Deliberate missing-worker fault, not a reproduction or explanation of the original intermittent CI failure.",
                "The accepted start and later snapshot RPC failure are separate RPCs; HTTP failure does not mean the start was rejected.",
                "Actual RPC status is retained without inferring its internal cause from elapsed time or a configured deadline.",
                "One explicit identical retry after observed worker readiness; no automatic retry or timeout increase.",
                "Automated fixture-account login, no person review or approval, and no publication activity.",
                "Loopback development Temporal, trusted filesystem and public two-page native/OCR fixture only."]}
        _save_report(out, report)
        return report
    finally:
        if browser is not None: browser.close()
        if portal is not None: portal.close()
        if resource is not None: resource.close()
        if human is not None: human.close()
        if identity is not None: identity.close()
        if env is not None: await env.shutdown()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("temporal-cli", "identity-dir", "model-dir", "manifest", "work-dir", "out"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--node", default="node")
    args = parser.parse_args(argv)
    try:
        report = asyncio.run(run_probe(args))
        print(json.dumps({"event": "completed", "controlled_fault": True,
            "initial_status": report["initial"]["http_status"], "recovery_status": report["recovery"]["http_status"]}))
        return 0
    except (ImportError, ValueError, OSError, RuntimeError, TimeoutError) as exc:
        print(f"Controlled review probe failed ({type(exc).__name__}); inspect the documented local setup.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
