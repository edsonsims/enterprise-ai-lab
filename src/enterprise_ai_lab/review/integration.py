"""Real local OIDC/browser-session protocol, PDFs, Temporal and RAG demonstration."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from html.parser import HTMLParser
from io import BytesIO
import json
from importlib.metadata import version
import os
import platform
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit

import httpx
from pypdf import PdfReader
from temporalio.api.enums.v1 import EventType
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from ..approval.bootstrap import verify_cli
from ..approval.contracts import format_utc
from ..publication.gateway import workflow_id
from ..publication.integration import LocalProcess
from ..publication.storage import CandidateRepository
from ..publication.workflow import DocumentPublicationWorkflow
from ..rag.integration import IdentityFixture, IntegrationError, _JsonProcess
from ..rag.models import verify_model
from ..rag.pdf_corpus import load_pdf_documents
from ..rag.pdf_demo import extraction_runtime
from .audit import AuditLog
from .failure import capture_failure, http_summary, phase_for_case, save_failure


class Form(HTMLParser):
    def __init__(self, text):
        super().__init__(); self.action = None; self.csrf = None
        self.feed(text)
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "form": self.action = values.get("action")
        if tag == "input" and values.get("name") == "csrf": self.csrf = values.get("value")


class HumanIssuer:
    def __init__(self, directory, node, redirect_uri, accounts, ttl=300):
        self.process = _JsonProcess([node, str(directory/"dist/review-bridge.js"),
            "--redirect-uri", redirect_uri, "--accounts-file", str(accounts), "--ttl-seconds", str(ttl)],
            cwd=directory, env=os.environ.copy())
        try:
            self.metadata = self.process.read()
            if (self.metadata.get("event") != "ready" or self.metadata.get("audience") != "urn:identity-boundary:review"
                    or self.metadata.get("client_id") != "review-portal"):
                raise IntegrationError("Browser identity fixture did not become ready.")
            from ..rag.auth import local_origin
            self.issuer = local_origin(self.metadata["issuer"])
        except BaseException:
            self.close(); raise
    def close(self): self.process.close(eof=True)


class Portal:
    def __init__(self, *, issuer, address, queue, database, audit, manifest, port, diagnostics_path=None):
        root = Path(__file__).resolve().parents[3]
        command = [sys.executable, "-m", "enterprise_ai_lab.review", "serve",
            "--issuer", issuer, "--address", address, "--task-queue", queue, "--database", str(database),
            "--audit-database", str(audit), "--manifest", str(manifest), "--port", str(port)]
        if diagnostics_path is not None:
            command += ["--diagnostics-file", str(diagnostics_path)]
        self.process = _JsonProcess(command,
            cwd=root, env={**os.environ, "PYTHONPATH": str(root/"src")})
        try:
            ready = self.process.read()
            if ready.get("event") != "ready": raise IntegrationError("Review portal did not become ready.")
            self.url = ready["url"]
        except BaseException:
            self.close(); raise
    def close(self): self.process.close()


class BrowserProtocol:
    """Actual HTTP cookie/redirect/form flow; distinct from a DOM browser check."""
    def __init__(self, origin, issuer):
        self.origin, self.issuer = origin, issuer
        self.client = httpx.Client(trust_env=False, follow_redirects=False, timeout=20)
        self.csrf = None
    def login(self, username, password):
        response = self.client.get(self.origin + "/login")
        for _ in range(10):
            if response.status_code in (302, 303, 307):
                target = urljoin(str(response.url), response.headers["location"])
                if f"{urlsplit(target).scheme}://{urlsplit(target).netloc}" not in (self.origin, self.issuer):
                    raise IntegrationError("Login left the explicit local origins.")
                response = self.client.get(target); continue
            if response.status_code == 200 and "/interaction/" in str(response.url):
                form = Form(response.text)
                if not form.action or not form.csrf: raise IntegrationError("Issuer login form missing.")
                target = urljoin(self.issuer, form.action)
                response = self.client.post(target, data={"csrf": form.csrf, "username": username, "password": password},
                    headers={"origin": self.issuer}); continue
            break
        if response.status_code != 200 or str(response.url) != self.origin + "/":
            raise IntegrationError("Actual browser-session login did not complete.")
        result = self.client.get(self.origin + "/api/session")
        if result.status_code != 200: raise IntegrationError("Signed-in browser session missing.")
        body = result.json(); self.csrf = body["csrf_token"]
        return {"authenticated": body["authenticated"], "user": body["user"], "expires_at": body["expires_at"]}
    def call(self, path, *, method="GET", body=None, csrf=True, origin=True):
        headers = {}
        if method == "POST":
            if origin: headers["origin"] = self.origin
            if csrf and self.csrf: headers["x-csrf-token"] = self.csrf
        return self.client.request(method, self.origin + path, headers=headers, json=body if method == "POST" else None)
    def close(self): self.client.close()


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); return probe.getsockname()[1]


async def run_demo(args):
    work, out = Path(args.work_dir).resolve(), Path(args.out).resolve()
    identity_dir, manifest, model_dir = Path(args.identity_dir).resolve(), Path(args.manifest).resolve(), Path(args.model_dir).resolve()
    work.mkdir(parents=True, exist_ok=True)
    if any(work.iterdir()): raise ValueError("choose a fresh work directory")
    cli, embedding = verify_cli(Path(args.temporal_cli)), verify_model(model_dir)
    documents = load_pdf_documents(manifest)
    candidates = CandidateRepository(work/"publication.sqlite3")
    alpha = candidates.stage([doc for doc in documents if doc.tenant_id == "alpha"])
    beta = candidates.stage([doc for doc in documents if doc.tenant_id == "beta"])
    native = candidates.stage([doc for doc in documents if doc.tenant_id == "alpha" and doc.provenance.method == "native"])
    users = ["requester-alpha", "reviewer-alpha", "analyst-reviewer-alpha", "requester-beta", "reviewer-beta", "reader-alpha", "observer-alpha"]
    passwords = {name: secrets.token_urlsafe(24) for name in users}
    accounts = work/"accounts.private.json"
    descriptor = os.open(accounts, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        json.dump({"schema_version": 1, "accounts": [{"username": k, "password": v} for k,v in passwords.items()]}, stream)
    address, queue = f"127.0.0.1:{free_port()}", "human-review-v1"
    portal_port = free_port(); origin = f"http://127.0.0.1:{portal_port}"
    service_identity = IdentityFixture(identity_dir, node=args.node)
    human = HumanIssuer(identity_dir, args.node, origin + "/auth/callback", accounts)
    env = resource = portal = None
    clients, observations, previews, histories = [], [], [], []
    audit_path = work/"review-audit.sqlite3"
    diagnostics_path = work/"portal-diagnostics.jsonl"
    failure_out = Path(getattr(args, "failure_out", None) or out.parent/"failure.json")
    failure_context = {"phase": "setup", "http": None}

    def require(condition, message):
        if not condition: raise IntegrationError(message)
    def client(username):
        failure_context.update(phase="login", http=None)
        browser = BrowserProtocol(origin, human.issuer); clients.append(browser)
        login = browser.login(username, passwords[username])
        require(login["user"]["subject"] == username, "OIDC subject was collapsed into the shared app client.")
        observations.append({"case": "login-"+username, "http_status": 200, "response": login})
        return browser
    def observe(browser, case, path, *, expected=200, **kwargs):
        failure_context.update(phase=phase_for_case(case), http=None)
        response = browser.call(path, **kwargs)
        if response.status_code != expected:
            failure_context["http"] = http_summary(response)
        require(response.status_code == expected, f"Review case {case} returned unexpected status {response.status_code}.")
        body = response.json()
        observations.append({"case": case, "http_status": response.status_code, "response": body})
        return body
    def create(browser, request_id, candidate, revision=0):
        return observe(browser, "create-"+request_id, "/api/reviews", method="POST", body={
            "request_id": request_id, "candidate_sha256": candidate.candidate_sha256,
            "expected_revision": revision, "expires_at": format_utc(datetime.now(timezone.utc)+timedelta(minutes=10))})
    def decision(browser, detail, verdict, command):
        return observe(browser, verdict+"-"+detail["request"]["request_id"],
            "/api/reviews/"+detail["request"]["request_id"]+"/decisions", method="POST", body={
                "command_id": command, "snapshot_sha256": detail["request"]["snapshot_sha256"], "decision": verdict})
    async def published(browser, request_id):
        deadline = time.monotonic()+40
        while time.monotonic()<deadline:
            response = browser.call("/api/reviews/"+request_id)
            if response.status_code == 200 and response.json()["publication_status"] == "published": return response.json()
            await asyncio.sleep(0.1)
        raise IntegrationError("Approved human review did not publish within its bounded wait.")
    def preview(browser, detail, document, variant="artifact"):
        failure_context.update(phase="preview", http=None)
        path = f"/api/reviews/{detail['request']['request_id']}/documents/{document['document_id']}/pdf"
        if variant == "original": path += "?variant=original"
        response = browser.call(path)
        require(response.status_code == 200 and response.headers.get("content-type") == "application/pdf", "Authorized PDF preview failed.")
        reader = PdfReader(BytesIO(response.content))
        require(len(reader.pages) == 1 and response.headers.get("x-preview-page-count") == "1", "Preview exposed ungranted PDF pages.")
        require(hashlib.sha256(response.content).hexdigest() == response.headers["x-preview-sha256"], "Preview bytes differ from response digest.")
        previews.append({"request_id": detail["request"]["request_id"], "document_id": document["document_id"],
            "variant": variant, "page_count": 1, "bytes": len(response.content),
            "sha256": response.headers["x-preview-sha256"], "provenance": document["provenance"]})
    try:
        env = await WorkflowEnvironment.start_local(port=int(address.split(":")[1]), ip="127.0.0.1",
            identity="human-review-study-recorder", dev_server_existing_path=str(Path(args.temporal_cli).resolve()),
            dev_server_database_filename=str(work/"temporal.sqlite3"), ui=False, dev_server_log_level="error")
        resource = LocalProcess("resource", issuer=service_identity.issuer, address=address, queue=queue,
            database=candidates.database_path, model_dir=model_dir, store=work/"vectors")
        portal = Portal(issuer=human.issuer,address=address,queue=queue,database=candidates.database_path,
            audit=audit_path,manifest=manifest,port=portal_port,diagnostics_path=diagnostics_path)
        unauth = BrowserProtocol(origin,human.issuer); clients.append(unauth)
        observe(unauth,"unauthenticated-api","/api/reviews",expected=401)
        requester = client("requester-alpha")
        main = create(requester,"review-public-pages",alpha)
        rejection = create(requester,"reject-public-pages",native)
        beta_requester = client("requester-beta")
        beta_review = create(beta_requester,"beta-public-pages",beta)
        reader = client("reviewer-alpha")
        partial = observe(reader,"partial-role-view","/api/reviews/review-public-pages")
        require(len(partial["documents"]) == 1 and partial["denied_document_count"] == 1 and not partial["can_decide"], "Reviewer role bypassed a page's reading ACL.")
        preview(reader,partial,partial["documents"][0])
        ocr_id = next(doc.document_id for doc in documents if doc.tenant_id == "alpha" and doc.provenance.method == "ocr")
        observe(reader,"hidden-ocr-pdf-denied",f"/api/reviews/review-public-pages/documents/{ocr_id}/pdf",expected=404)
        observe(reader,"blind-approval-denied","/api/reviews/review-public-pages/decisions",method="POST",
            body={"command_id":"blind-review","snapshot_sha256":main["request"]["snapshot_sha256"],"decision":"approve"},expected=403)
        reviewer = client("analyst-reviewer-alpha")
        detail = observe(reviewer,"complete-role-view","/api/reviews/review-public-pages")
        require(len(detail["documents"])==2 and detail["can_decide"], "Authorized complete reviewer could not inspect both pages.")
        for document in detail["documents"]:
            text = document["text"]
            for chunk in document["chunks"]:
                span=chunk["provenance"]
                require(text[span["start"]:span["end"]]==chunk["text"],"Review chunk lost its exact canonical source span.")
            preview(reviewer,detail,document)
            if document["provenance"]["method"]=="ocr": preview(reviewer,detail,document,"original")
        observe(reviewer,"csrf-denied","/api/reviews/review-public-pages/decisions",method="POST",csrf=False,
            body={"command_id":"csrf-review","snapshot_sha256":detail["request"]["snapshot_sha256"],"decision":"approve"},expected=403)
        observe(reviewer,"cross-origin-denied","/api/reviews/review-public-pages/decisions",method="POST",origin=False,
            body={"command_id":"origin-review","snapshot_sha256":detail["request"]["snapshot_sha256"],"decision":"approve"},expected=403)
        beta_reader=client("reviewer-beta")
        observe(beta_reader,"cross-tenant-detail-denied","/api/reviews/review-public-pages",expected=404)
        listed=observe(beta_reader,"beta-queue-isolated","/api/reviews")
        require({v["request"]["request_id"] for v in listed["reviews"]}=={"beta-public-pages"},"Queue crossed signed tenant.")
        observer=client("observer-alpha")
        observe(observer,"observer-role-denied","/api/reviews",expected=403)
        code, body=resource.query(service_identity.token("reader-alpha"),"alpha","How is access to individual enterprise resources granted?")
        require(code==200 and body["abstained"] and not body["citations"],"Pending review reached RAG.")
        observations.append({"case":"pending-rag-empty","http_status":code,"response":body})
        decision(reviewer,rejection,"reject","review-reject-1")
        decision(reviewer,detail,"approve","review-approve-1")
        done=await published(reviewer,"review-public-pages")
        observations.append({"case":"human-account-approved-publication","http_status":200,"response":done})
        decision(reviewer,detail,"approve","review-approve-1")
        decision(beta_reader,beta_review,"approve","beta-review-1")
        await published(beta_reader,"beta-public-pages")
        code, body=resource.query(service_identity.token("analyst-alpha"),"alpha","What functions is the Core composed of?")
        require(code==200 and not body["abstained"] and any(c["provenance"]["method"]=="ocr" for c in body["citations"]),"Approved OCR source did not reach scoped RAG.")
        observations.append({"case":"approved-ocr-rag","http_status":code,"response":body})
        before=observe(reviewer,"audit-before-restart","/api/audit")
        portal.close(); portal=None
        portal=Portal(issuer=human.issuer,address=address,queue=queue,database=candidates.database_path,
            audit=audit_path,manifest=manifest,port=portal_port,diagnostics_path=diagnostics_path)
        observe(reviewer,"old-session-denied-after-portal-restart","/api/session",expected=401)
        reviewer=client("analyst-reviewer-alpha")
        after=observe(reviewer,"audit-after-restart","/api/audit")
        prior_hashes={v["event_sha256"] for v in before["events"]}
        require(prior_hashes <= {v["event_sha256"] for v in after["events"]},"Restart lost audited decisions.")
        observe(reviewer,"logout","/logout",method="POST",body={})
        observe(reviewer,"logged-out-denied","/api/reviews",expected=401)

        browser_result=None
        if getattr(args,"browser_test",False):
            requester = client("requester-alpha")
            beta_requester = client("requester-beta")
            create(requester,"browser-main",alpha,revision=1)
            create(requester,"browser-reject",native,revision=1)
            create(beta_requester,"browser-beta",beta,revision=1)
            browser_out=work/"browser-checks.json"
            failure_context.update(phase="browser", http=None)
            root=Path(__file__).resolve().parents[3]
            process=await asyncio.create_subprocess_exec(args.node,str(root/"tests/review/browser.mjs"),origin,str(accounts),str(browser_out),
                cwd=root,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            stdout,stderr=await asyncio.wait_for(process.communicate(),timeout=180)
            require(process.returncode==0,"Actual Chromium review interface checks failed; inspect private local test output.")
            browser_result=json.loads(browser_out.read_text())
            out.parent.mkdir(parents=True,exist_ok=True)
            for name in ("browser-review.png", "browser-review-mobile.png"):
                shutil.copyfile(work/name,out.parent/name)
            (out.parent/"browser.json").write_text(json.dumps(browser_result,ensure_ascii=False,indent=2)+"\n")

        effects=candidates.list_effects()
        expected_effects=4 if browser_result else 2
        require(len(effects)==expected_effects,"Review replay or rejection changed publication count.")
        out.parent.mkdir(parents=True,exist_ok=True)
        replayer=Replayer(workflows=[DocumentPublicationWorkflow])
        failure_context.update(phase="replay", http=None)
        pairs=[("alpha","review-public-pages"),("alpha","reject-public-pages"),("beta","beta-public-pages")]
        if browser_result: pairs += [("alpha","browser-main"),("alpha","browser-reject"),("beta","browser-beta")]
        for tenant,request_id in pairs:
            handle=env.client.get_workflow_handle(workflow_id(tenant,request_id))
            await handle.result(rpc_timeout=timedelta(seconds=5)); history=await handle.fetch_history(rpc_timeout=timedelta(seconds=3))
            await replayer.replay_workflow(history)
            name=f"histories/{tenant}-{request_id}.json"; path=out.parent/name;path.parent.mkdir(parents=True,exist_ok=True)
            raw=(history.to_json()+"\n").encode();path.write_bytes(raw)
            histories.append({"tenant_id":tenant,"request_id":request_id,"filename":name,"sha256":hashlib.sha256(raw).hexdigest(),"event_count":len(history.events),"event_types":[EventType.Name(e.event_type) for e in history.events]})
        require(candidates.list_effects()==effects,"History replay published another document.")
        audit=AuditLog(audit_path).events()
        require(any(v["action"]=="decision_accepted" and v["identity_kind"]=="user" for v in audit),"Signed-in account receipt missing from audit.")
        result={"schema_version":1,"recorded_at":datetime.now(timezone.utc).isoformat(),
            "experiment":"Actual local OIDC user-account login, permission-aware PDF/snippet review, durable decisions and RAG publication",
            "cli":cli,"embedding":embedding,
            "runtime":{**extraction_runtime(), "machine":platform.machine(), "temporalio":version("temporalio"),
                "onnxruntime":version("onnxruntime"), "qdrant-client":version("qdrant-client"),
                "node":subprocess.run([args.node,"--version"],check=True,capture_output=True,timeout=5).stdout.decode().strip()},
            "identity_bridge_sha256":hashlib.sha256((identity_dir/"dist/review-bridge.js").read_bytes()).hexdigest(),
            "identity_issuer_sha256":hashlib.sha256((identity_dir/"dist/review-issuer.js").read_bytes()).hexdigest(),
            "oidc":{"flow":"authorization_code","pkce":"S256","state_and_nonce_verified":True,"access_token_seconds":300,"sessions":"opaque HTTP-only cookies; tokens in server memory; restart invalidates sessions"},
            "observations":observations,"previews":previews,"effects":effects,"audit":audit,"audit_chain_valid":True,
            "histories":histories,"replay":{"all_passed":True,"no_new_effects":True},"browser":browser_result,
            "human_review_performed":False,"limitations":["Automated fixture account logins and clicks do not prove a person read, understood or approved a source.",
                "Loopback HTTP-only lab; host administrator, development Temporal gRPC and source manifests are trusted.",
                "Hash-linked local audit is not externally anchored or tamper-proof against host administration; intent/receipt reconciliation is not a distributed transaction.",
                "Only individually authorized pages are previewed as labelled derivatives; whole-file downloads are not granted.",
                "Sessions expire with user tokens and vanish on portal restart; refresh grants, external identity, production HTTPS, key rotation and live entitlement sync remain planned.",
                "Two public English NIST pages and authored tenant/role grants; no new OCR or answer-quality benchmark."]}
        encoded=json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False)+"\n";out.write_text(encoded)
        print(json.dumps({"event":"completed","output":str(out),"observations":len(observations),"audit_events":len(audit),"browser_tested":bool(browser_result)}),flush=True)
        if getattr(args,"stay_open",False):
            # These are separate manual exercises, excluded from the frozen
            # automated report and from any claim that a person approved it.
            interactive_requester = client("requester-alpha")
            create(interactive_requester,"hands-on-review",alpha,revision=candidates.revision("alpha"))
            create(interactive_requester,"hands-on-reject",native,revision=candidates.revision("alpha"))
            print(json.dumps({"event":"ready","url":origin,"accounts_file":str(accounts),"fixture_only":True}),flush=True)
            await asyncio.Event().wait()
        return json.loads(encoded)
    except BaseException:
        # Export only validated finite metadata before child pipes/private state
        # disappear. A diagnostic failure must not hide the original exception.
        saved = False
        try:
            summary = capture_failure(phase=failure_context["phase"], http=failure_context["http"],
                diagnostics_path=diagnostics_path, portal=None if portal is None else portal.process,
                resource=None if resource is None else resource.process)
            save_failure(failure_out, summary)
            saved = True
        except Exception:
            pass
        print(json.dumps({"event": "failure_diagnostics", "saved": saved,
            "phase": failure_context["phase"]}), flush=True)
        raise
    finally:
        for browser in clients: browser.close()
        if portal is not None: portal.close()
        if resource is not None: resource.close()
        if env is not None: await env.shutdown()
        human.close(); service_identity.close()
