"""Actual persistent Temporal + OAuth + HTTP crash/restart/replay study.

Only synthetic local notes are used. Fixture service identities simulate a
reviewer; no person is authenticated and no human approval is claimed.
"""
from __future__ import annotations

import asyncio
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

from ..rag.auth import APPROVAL_AUDIENCE, local_origin
from ..rag.integration import IdentityFixture, IntegrationError, _JsonProcess
from .activities import LocalNotifier
from .bootstrap import verify_cli
from .contracts import format_utc
from .gateway import workflow_id
from .workflow import ApprovalWorkflow


class WorkerProcess:
    def __init__(self, address, queue, database, *, pause=0):
        root = Path(__file__).resolve().parents[3]
        command = [sys.executable, "-m", "enterprise_ai_lab.approval", "worker", "--address", address,
            "--task-queue", queue, "--notifier-db", str(Path(database).resolve()), "--pause-after-commit", str(pause)]
        self.process = _JsonProcess(command, cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")})
        try:
            if self.process.read() != {"event": "ready", "worker": "approval"}:
                raise IntegrationError("Approval worker did not become ready.")
        except BaseException:
            self.close(); raise

    def kill(self):
        self.process._child.kill()
        self.process._child.wait(timeout=3)
        self.close()

    def close(self):
        self.process.close()


class EdgeProcess:
    def __init__(self, issuer, address, queue):
        root = Path(__file__).resolve().parents[3]
        command = [sys.executable, "-m", "enterprise_ai_lab.approval", "serve", "--issuer", issuer,
            "--address", address, "--task-queue", queue, "--port", "0"]
        self.process = _JsonProcess(command, cwd=root, env={**os.environ, "PYTHONPATH": str(root / "src")})
        self.client = httpx.Client(timeout=20, trust_env=False, follow_redirects=False)
        try:
            ready = self.process.read()
            if ready.get("event") != "ready":
                raise IntegrationError("Approval edge did not become ready.")
            self.url = local_origin(ready["url"])
            if self.client.get(self.url + "/healthz").status_code != 200:
                raise IntegrationError("Approval edge did not become healthy.")
        except BaseException:
            self.close(); raise

    def call(self, token, *, tenant="alpha", request_id=None, decision=False, method="POST", body=None):
        path = f"/v1/tenants/{tenant}/approvals"
        if request_id is not None:
            path += "/" + request_id
        if decision:
            path += "/decisions"
        response = self.client.request(method, self.url + path,
            headers={"authorization": "Bearer " + token} if token else {}, json=body if method == "POST" else None)
        result = response.json()
        if not isinstance(result, dict) or (token and token in json.dumps(result)):
            raise IntegrationError("Approval edge returned an invalid or credential-reflecting response.")
        return response.status_code, result

    def close(self):
        self.client.close(); self.process.close()


async def run_demo(temporal_cli: Path, identity_dir: Path, node: str, work_dir: Path, out: Path) -> dict:
    cli = verify_cli(temporal_cli)
    work_dir, out, identity_dir = Path(work_dir).resolve(), Path(out).resolve(), Path(identity_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    if any(work_dir.iterdir()):
        raise ValueError("choose a fresh work directory; prior durable records are preserved")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    address, queue = f"127.0.0.1:{port}", "approval-demo-v1"
    server_db, note_db = work_dir / "temporal.sqlite3", work_dir / "notifications.sqlite3"
    notifier = LocalNotifier(note_db)
    identity = IdentityFixture(identity_dir, node=node)
    env = worker = edge = None
    observations, histories = [], []

    async def host():
        return await WorkflowEnvironment.start_local(port=port, ip="127.0.0.1", identity="approval-study-recorder",
            dev_server_existing_path=str(Path(temporal_cli).resolve()),
            dev_server_database_filename=str(server_db), ui=False,
            dev_server_log_level="error")

    def require(condition, message):
        if not condition:
            raise IntegrationError(message)

    def token(client="requester-alpha", *, resource=APPROVAL_AUDIENCE, scope=None):
        return identity.token(client, resource=resource, scope=scope)

    def observe(case, *, client="requester-alpha", expected=200, token_value=None, **kwargs):
        credential = token_value if token_value is not None else token(client)
        status, body = edge.call(credential, **kwargs)
        require(status == expected, f"Approval case {case} returned unexpected HTTP status {status}.")
        observations.append({"case": case, "http_status": status, "response": body})
        return body

    def request(request_id, *, lifetime=300):
        return {"request_id": request_id, "note": {"title": "Synthetic study note", "body": "Publish this fixture only to the local SQLite notifier."},
            "expires_at": format_utc(datetime.now(timezone.utc) + timedelta(seconds=lifetime))}

    async def settled(request_id, status, publication=None, *, tenant="alpha", client="requester-alpha", timeout=45):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            code, result = edge.call(token(client), tenant=tenant, request_id=request_id, method="GET")
            if code == 200 and result["status"] == status and (publication is None or result["publication_status"] == publication):
                return result
            if code not in (200, 503):
                raise IntegrationError("Approval status polling returned an unexpected status.")
            await asyncio.sleep(0.1)
        raise IntegrationError("Approval workflow did not settle within the explicit study deadline.")

    primary, dual, rejected, expired = request("publish-study-note"), request("self-review"), request("reject-study-note"), None
    try:
        env = await host()
        worker = WorkerProcess(address, queue, note_db)
        edge = EdgeProcess(identity.issuer, address, queue)
        created = observe("request-created", body=primary)
        digest = created["request"]["snapshot_sha256"]
        require(created["request"]["requested_by"] == "requester-alpha" and created["status"] == "pending",
            "Request did not preserve verified requester and pending state.")
        observe("exact-create-replay", body=primary)
        observe("changed-create-conflict", body={**primary, "note": {"title": "Changed", "body": "Different content"}}, expected=409)
        decision = {"command_id": "review-1", "snapshot_sha256": digest, "decision": "approve"}
        valid = token("approver-alpha")
        header, payload, signature = valid.split(".")
        forged = header + "." + payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:]
        observe("forged-token-denied", token_value=forged, request_id=primary["request_id"], decision=True, body=decision, expected=401)
        observe("rag-audience-denied", token_value=token("analyst-alpha", resource="urn:identity-boundary:rag"),
            request_id=primary["request_id"], decision=True, body=decision, expected=401)
        observe("cross-tenant-denied", client="approver-beta", request_id=primary["request_id"], decision=True, body=decision, expected=403)
        observe("wrong-role-denied", client="reader-alpha", request_id=primary["request_id"], decision=True, body=decision, expected=403)
        observe("missing-scope-denied", token_value=token("approver-alpha", scope=""),
            request_id=primary["request_id"], decision=True, body=decision, expected=403)
        observe("tampered-snapshot-denied", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**decision, "snapshot_sha256": "0" * 64}, expected=409)
        observe("body-actor-spoof-denied", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**decision, "actor_id": "someone-else"}, expected=422)
        observe("unknown-request", request_id="missing", method="GET", expected=404)
        self_request = observe("dual-role-request-created", client="dual-alpha", body=dual)
        observe("self-approval-denied", client="dual-alpha", request_id=dual["request_id"], decision=True,
            body={**decision, "snapshot_sha256": self_request["request"]["snapshot_sha256"]}, expected=409)
        require(not notifier.list_notifications(), "A local notification appeared before any approval.")

        # Shut down every process that could cache workflow state, then reopen
        # the same actual native Temporal database while it is still pending.
        edge.close(); worker.close(); edge = worker = None
        await env.shutdown(); env = await host()
        worker = WorkerProcess(address, queue, note_db, pause=15)
        edge = EdgeProcess(identity.issuer, address, queue)
        restored = observe("pending-after-server-worker-edge-restart", request_id=primary["request_id"], method="GET")
        require(restored["request"] == created["request"] and restored["status"] == "pending" and not notifier.list_notifications(),
            "Pending request did not survive persistent server/worker/edge restart.")
        accepted = observe("approve-before-effect-ack", client="approver-alpha", request_id=primary["request_id"], decision=True, body=decision)
        require(accepted["receipt"]["command"]["actor_id"] == "approver-alpha", "Verified approver identity was not retained.")
        deadline = time.monotonic() + 8
        while not notifier.list_notifications() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        rows = notifier.list_notifications()
        require(len(rows) == 1 and rows[0]["request_id"] == primary["request_id"], "No committed local effect observed before crash.")
        before_crash = await env.client.get_workflow_handle(workflow_id("alpha", primary["request_id"])).fetch_history(rpc_timeout=timedelta(seconds=3))
        require(not any(e.event_type == EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED for e in before_crash.events),
            "Crash probe missed the after-commit/before-ack window.")
        worker.kill(); worker = None
        edge.close(); edge = None
        await env.shutdown(); env = await host()
        worker = WorkerProcess(address, queue, note_db)
        edge = EdgeProcess(identity.issuer, address, queue)
        published = await settled(primary["request_id"], "approved", "published")
        observations.append({"case": "published-once-after-worker-kill-and-server-restart", "http_status": 200, "response": published})
        observe("exact-decision-replay", client="approver-alpha", request_id=primary["request_id"], decision=True, body=decision)
        observe("changed-command-id-payload-conflict", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**decision, "decision": "reject"}, expected=409)
        observe("changed-actor-same-command-conflict", client="dual-alpha", request_id=primary["request_id"], decision=True,
            body=decision, expected=409)
        observe("changed-digest-same-command-conflict", client="approver-alpha", request_id=primary["request_id"], decision=True,
            body={**decision, "snapshot_sha256": "0" * 64}, expected=409)

        # The same caller-chosen request ID is independent in another tenant.
        beta = observe("same-request-id-other-tenant", client="requester-beta", tenant="beta", body=primary)
        require(beta["request"]["snapshot_sha256"] != digest, "Tenant identity was not bound into the snapshot.")
        observe("beta-approved", client="approver-beta", tenant="beta", request_id=primary["request_id"], decision=True,
            body={**decision, "snapshot_sha256": beta["request"]["snapshot_sha256"]})
        await settled(primary["request_id"], "approved", "published", tenant="beta", client="requester-beta")

        no = observe("rejection-request-created", body=rejected)
        observe("rejected", client="approver-alpha", request_id=rejected["request_id"], decision=True,
            body={"command_id": "reject-1", "snapshot_sha256": no["request"]["snapshot_sha256"], "decision": "reject"})
        await settled(rejected["request_id"], "rejected", "not_applicable")
        observe("independent-reviewer-rejects-self-request", client="approver-alpha", request_id=dual["request_id"], decision=True,
            body={"command_id": "dual-reject", "snapshot_sha256": self_request["request"]["snapshot_sha256"], "decision": "reject"})
        await settled(dual["request_id"], "rejected", "not_applicable", client="dual-alpha")

        expired = request("expire-study-note", lifetime=4)
        exp = observe("expiry-request-created", body=expired)
        worker.close(); worker = None
        # Use the real durable timer and clock, not Temporal time skipping.
        await asyncio.sleep(4.2)
        worker = WorkerProcess(address, queue, note_db)
        timeout = await settled(expired["request_id"], "expired", "not_applicable")
        observations.append({"case": "expired-while-worker-absent", "http_status": 200, "response": timeout})
        observe("late-decision-denied", client="approver-alpha", request_id=expired["request_id"], decision=True,
            body={"command_id": "late-1", "snapshot_sha256": exp["request"]["snapshot_sha256"], "decision": "approve"}, expected=409)
        edge.close(); edge = EdgeProcess(identity.issuer, address, queue)
        observe("published-after-edge-restart", request_id=primary["request_id"], method="GET")
        effects = notifier.list_notifications()
        require(len(effects) == 2 and sum(row["tenant_id"] == "alpha" and row["request_id"] == primary["request_id"] for row in effects) == 1,
            "Crash/retry duplicated a local effect or published a rejected/expired note.")

        out.parent.mkdir(parents=True, exist_ok=True)
        replayer = Replayer(workflows=[ApprovalWorkflow])
        for tenant, request_id in [("alpha", primary["request_id"]), ("beta", primary["request_id"]),
            ("alpha", rejected["request_id"]), ("alpha", dual["request_id"]), ("alpha", expired["request_id"])]:
            handle = env.client.get_workflow_handle(workflow_id(tenant, request_id))
            await handle.result(rpc_timeout=timedelta(seconds=5))
            history = await handle.fetch_history(rpc_timeout=timedelta(seconds=3))
            await replayer.replay_workflow(history)
            filename = f"histories/{tenant}-{request_id}.json"
            path = out.parent / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            raw = (history.to_json() + "\n").encode()
            path.write_bytes(raw)
            histories.append({"request_id": request_id, "tenant_id": tenant, "filename": filename,
                "sha256": hashlib.sha256(raw).hexdigest(), "event_count": len(history.events),
                "event_types": [EventType.Name(e.event_type) for e in history.events]})
        require(notifier.list_notifications() == effects, "History replay executed another local side effect.")
        result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
            "experiment": "Actual persistent Temporal approval, verified OAuth service identities, HTTP and local SQLite effects",
            "runtime": {"python": platform.python_version(), "platform": platform.system(),
                "temporalio": version("temporalio"), "cli": cli,
                "node": subprocess.run([node, "--version"], check=True, capture_output=True, timeout=5).stdout.decode().strip(),
                "identity_bridge_sha256": hashlib.sha256((identity_dir / "dist/bridge.js").read_bytes()).hexdigest()},
            "observations": observations, "histories": histories,
            "durability": {"worker_killed_after_commit": True, "server_restarted": True,
                "edge_restarted": True, "notification_rows_for_primary": 1,
                "total_notifications": len(effects), "pending_request_preserved": True,
                "expiry_while_worker_absent": True, "before_crash_activity_completed": False},
            "local_notifications": effects,
            "replay": {"histories": len(histories), "all_passed": True, "no_new_local_effects": True},
            "human_review_performed": False,
            "limitations": ["Client-credentials fixture identities simulate a reviewer; no human login or owner approval is recorded.",
                "The Temporal development gRPC endpoint is trusted local administration and bypasses HTTP authorization.",
                "The only effect is a synthetic local SQLite row; no email, deployment or external action.",
                "Effect deduplication requires the preserved local database; Temporal update/request deduplication is bounded by retained history.",
                "This development server, offline key snapshot and workflow code have no production operations/version-migration validation."]}
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return result
    finally:
        if edge is not None: edge.close()
        if worker is not None: worker.close()
        if env is not None: await env.shutdown()
        identity.close()
