"""Actual local issuer, resource process, SQLite vault and synthetic HTTP processor."""
from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import threading
import time

import httpx

from ..rag.auth import PRIVACY_AUDIENCE, local_origin, strict_json
from ..rag.integration import IdentityFixture, IntegrationError
from .vault import Vault


def require(condition):
    if not condition:
        raise IntegrationError("A bounded privacy experiment assertion failed.")


class SyntheticProcessor(AbstractContextManager):
    """A real loopback HTTP tool, deliberately not a neural model or external provider."""
    def __init__(self):
        self.calls = []
        self.mode = "normal"
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    size = int(self.headers.get("content-length", "0"))
                    if self.path != "/process" or not 0 < size <= 65536:
                        raise ValueError()
                    body = strict_json(self.rfile.read(size))
                    if not isinstance(body, dict) or set(body) != {"text"} or not isinstance(body["text"], str):
                        raise ValueError()
                    owner.calls.append({"text": body["text"], "authorization_present": "authorization" in self.headers})
                    if owner.mode == "error":
                        self.send_response(503); self.end_headers(); return
                    output = "Processed: " + body["text"]
                    if owner.mode == "unknown_token":
                        output += " [[PII:" + "0" * 32 + "]]"
                    raw = json.dumps({"text": output}, ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(raw)))
                    self.end_headers(); self.wfile.write(raw)
                except Exception:
                    self.send_response(400); self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.origin = f"http://127.0.0.1:{self.server.server_address[1]}"

    def __exit__(self, *_args):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)


class PrivacyServer(AbstractContextManager):
    def __init__(self, issuer, database, logs, *, processor=None, ttl=120):
        self.database = Path(database)
        self.logs = Path(logs)
        self.logs.mkdir(mode=0o700)
        self.stdout_path, self.stderr_path = self.logs / "stdout.log", self.logs / "stderr.log"
        self._handles = [os.fdopen(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb")
                         for p in (self.stdout_path, self.stderr_path)]
        root = Path(__file__).resolve().parents[3]
        command = [sys.executable, "-m", "enterprise_ai_lab.privacy_proxy", "serve", "--issuer", issuer,
                   "--vault", str(self.database), "--ttl-seconds", str(ttl), "--port", "0"]
        if processor:
            command += ["--processor", processor]
        self.child = subprocess.Popen(command, cwd=root, stdout=self._handles[0], stderr=self._handles[1],
            env={**os.environ, "PYTHONPATH": str(root / "src")})
        self.client = httpx.Client(timeout=5, trust_env=False, follow_redirects=False)
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                raw = self.stdout_path.read_bytes()
                if len(raw) > 65536 or self.child.poll() is not None:
                    raise IntegrationError("Privacy resource did not start safely.")
                if b"\n" in raw:
                    ready = strict_json(raw.splitlines()[0])
                    require(set(ready) == {"event", "url"} and ready["event"] == "ready")
                    self.url = local_origin(ready["url"])
                    if self.client.get(self.url + "/healthz").status_code == 200:
                        return
                time.sleep(.025)
            raise IntegrationError("Privacy resource readiness deadline expired.")
        except BaseException:
            self.close(); raise

    def call(self, token, operation, *, tenant="alpha", body=None, headers=None, raw=None):
        h = {"authorization": "Bearer " + token} if token else {}
        h.update(headers or {})
        if raw is not None:
            h.setdefault("content-type", "application/json")
        response = self.client.post(self.url + f"/v1/tenants/{tenant}/privacy/{operation}",
            headers=h, content=raw, json=body if raw is None else None)
        value = response.json()
        require(isinstance(value, dict) and response.headers.get("cache-control") == "no-store")
        require(not token or token not in json.dumps(value))
        return response.status_code, value

    def close(self):
        self.client.close()
        if self.child.poll() is None:
            self.child.terminate()
            try:
                self.child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.child.kill(); self.child.wait(timeout=2)
        for h in self._handles:
            h.close()

    def __exit__(self, *_args):
        self.close()


def run_demo(identity_dir: Path, node: str, work_dir: Path, out: Path) -> dict:
    work_dir, out = Path(work_dir).resolve(), Path(out).resolve()
    if work_dir.exists():
        raise ValueError("Choose a new work directory; prior vaults are preserved.")
    if os.path.lexists(out):
        raise ValueError("Choose a new output path; prior reports are preserved.")
    work_dir.mkdir(parents=True, mode=0o700)
    observations, credentials, processes = [], [], []
    source = "Fictional note for Mara Vale: nina@privacy.example, +1 202-555-0114, card 4242 4242 4242 4242."
    values = ["nina@privacy.example", "+1 202-555-0114", "4242 4242 4242 4242"]
    database = work_dir / "vault" / "maps.sqlite3"
    server = None
    with IdentityFixture(identity_dir, node=node) as identity, SyntheticProcessor() as processor:
        def token(profile="privacy-alpha", *, resource=PRIVACY_AUDIENCE, scope=None):
            value = identity.token(profile, resource=resource, scope=scope)
            credentials.append(value)
            return value

        def start(label, *, db=database, ttl=120):
            child = PrivacyServer(identity.issuer, db, work_dir / label, processor=processor.origin, ttl=ttl)
            processes.append(child)
            return child

        def observe(case, op, *, expected=200, profile="privacy-alpha", tenant="alpha", credential=None, **kwargs):
            code, response = server.call(token(profile) if credential is None else credential, op, tenant=tenant, **kwargs)
            require(code == expected)
            observations.append({"case": case, "http_status": code, "response": response})
            return response

        try:
            server = start("first")
            primary = observe("redact", "redactions", body={"text": source})
            require(len(primary["detections"]) == 3 and all(v not in primary["redacted_text"] for v in values))
            restore_body = {"context_id": primary["context_id"], "text": primary["redacted_text"]}
            require(observe("restore", "restorations", body=restore_body)["restored_text"] == source)
            observe("cross_tenant_path", "restorations", profile="privacy-beta", body=restore_body, expected=403)
            observe("other_tenant_context", "restorations", profile="privacy-beta", tenant="beta", body=restore_body, expected=404)
            observe("other_client_same_tenant", "restorations", profile="privacy-other-alpha", body=restore_body, expected=404)
            observe("redactor_cannot_restore", "restorations", profile="privacy-redactor-alpha", body=restore_body, expected=403)
            observe("missing_restore_scope", "restorations", credential=token(scope="privacy:redact"), body=restore_body, expected=403)
            observe("wrong_audience", "redactions", credential=token("reader-alpha", resource="urn:identity-boundary:rag"), body={"text": source}, expected=401)
            observe("missing_token", "redactions", credential="", body={"text": source}, expected=401)
            observe("identity_override", "redactions", headers={"x-tenant-id": "beta"}, body={"text": source}, expected=400)
            observe("extra_body_identity", "redactions", body={"text": source, "tenant_id": "beta"}, expected=400)
            observe("duplicate_json", "redactions", raw=b'{"text":"a","text":"b"}', expected=400)
            observe("invalid_utf8", "redactions", raw=b'{"text":"\xff"}', expected=400)
            observe("body_limit", "redactions", raw=b'{"text":"' + b"x" * 70000 + b'"}', expected=413)
            observe("reserved_literal", "redactions", body={"text": "literal [[PII:" + "a"*32 + "]]"}, expected=400)
            another = observe("same_values_new_context", "redactions", body={"text": source})
            require(primary["context_id"] != another["context_id"] and primary["redacted_text"] != another["redacted_text"])
            observe("copied_other_context_token", "restorations", body={"context_id": primary["context_id"], "text": another["redacted_text"]}, expected=400)
            processed = observe("actual_http_processor", "process", body={"text": source})
            require(processed["restored_text"] == "Processed: " + source)
            processor.mode = "unknown_token"
            denied = observe("processor_injected_token", "process", body={"text": source}, expected=400)
            require(all(v not in json.dumps(denied) for v in values))
            processor.mode = "error"
            observe("processor_failure", "process", body={"text": source}, expected=502)
            processor.mode = "normal"
            lock = sqlite3.connect(database, timeout=.1)
            try:
                lock.execute("BEGIN EXCLUSIVE")
                started = time.monotonic()
                observe("actual_sqlite_lock", "restorations", body=restore_body, expected=503)
                busy_seconds = time.monotonic() - started
                require(busy_seconds < 2)
            finally:
                lock.rollback(); lock.close()
            require(observe("after_lock_released", "restorations", body=restore_body)["restored_text"] == source)
            server.close()
            server = start("restart")
            require(observe("restore_after_resource_restart", "restorations", body=restore_body)["restored_text"] == source)
            server.close()
            server = start("expiry", db=work_dir / "expiry-vault" / "maps.sqlite3", ttl=1)
            expires = observe("short_context_created", "redactions", body={"text": source})
            time.sleep(1.1)
            observe("actual_context_expiry", "restorations", body={"context_id": expires["context_id"], "text": expires["redacted_text"]}, expected=404)
        finally:
            for child in processes:
                child.close()

        log_bytes = b"".join(p.read_bytes() for child in processes for p in (child.stdout_path, child.stderr_path))
        require(len(log_bytes) <= 65536)
        sensitive = values + credentials + [source]
        require(all(value.encode() not in log_bytes for value in sensitive))
        require(all(not call["authorization_present"] and all(v not in call["text"] for v in values) for call in processor.calls))
        require(any("Mara Vale" in call["text"] for call in processor.calls))
        with_vault = Vault(database)
        try:
            audit = with_vault.audit_events()
        finally:
            with_vault.close()
        require(all(value not in json.dumps(audit) for value in values + credentials))
        root = Path(__file__).resolve().parents[1]
        paths = [*sorted((root / "privacy_proxy").glob("*.py")), root / "rag" / "auth.py"]
        identity_paths = [Path(identity_dir) / "dist" / file for file in ("issuer.js", "bridge.js")]
        report = {"schema": "privacy-process-v1", "recorded_at": datetime.now(timezone.utc).isoformat(),
            "synthetic_only": True, "processor": "actual loopback HTTP deterministic transformer; no model call",
            "runtime": {"python": platform.python_version(), "platform": platform.platform(),
                        "httpx": version("httpx"), "PyJWT": version("PyJWT"),
                        "node": subprocess.run([node, "--version"], capture_output=True, text=True, check=True).stdout.strip()},
            "source_sha256": {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
            "identity_dist_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in identity_paths},
            "synthetic_input": source, "observations": observations, "processor_requests": processor.calls,
            "limitations": {"unsupported_name_was_forwarded": True, "universal_pii_detection": False,
                            "vault_encrypted": False, "secure_physical_erasure": False,
                            "issuer_remained_running_during_resource_restart": True,
                            "external_processor_or_neural_inference": False},
            "actual_sqlite_busy_seconds": busy_seconds,
            "log_scan": {"resource_processes": len(processes), "bytes": len(log_bytes),
                         "matched_supported_values_or_bearer_tokens": 0, "raw_private_issuer_ipc_included": False},
            "processes_reaped": all(child.child.poll() is not None for child in processes),
            "audit": audit}
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
        return report
