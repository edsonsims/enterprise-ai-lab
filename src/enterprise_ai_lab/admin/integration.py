"""Actual offline account login, HTTP, SQLite restart and controlled crash demo."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
from importlib.metadata import version
import json
import os
from pathlib import Path
import platform
import secrets
import socket
import stat
import subprocess
import sys
import time
from urllib.parse import urljoin, urlsplit

import httpx

from ..mcp_gateway.store import initialize_fixture
from ..rag.auth import local_origin
from ..rag.integration import IntegrationError, _JsonProcess

_BROWSER_STAGES = frozenset(
    (
        "launch",
        "alpha_login",
        "login_navigation",
        "login_form",
        "login_redirect",
        "login_view",
        "login_session",
        "initial_view_guard",
        "keyboard_update",
        "rollback",
        "saved_command_recovery",
        "desktop_pixels",
        "auditor",
        "beta",
        "mobile",
    )
)
_BROWSER_CODES = frozenset(("browser_nonzero", "browser_timeout", "report_unavailable"))


class BrowserFailure(IntegrationError):
    """Finite diagnostic metadata only; never child output or exception text."""

    def __init__(self, code, stage="unknown", returncode=None):
        self.code = (
            code
            if type(code) is str and code in _BROWSER_CODES
            else "report_unavailable"
        )
        self.failure_stage = (
            stage if type(stage) is str and stage in _BROWSER_STAGES else "unknown"
        )
        self.returncode = (
            returncode
            if type(returncode) is int and -255 <= returncode <= 255
            else None
        )
        super().__init__(
            f"Admin browser verification failed: code={self.code}; stage={self.failure_stage}; returncode={self.returncode}."
        )


def _private_browser_report(path):
    """Return bounded private report data or None; errors never reveal paths."""
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_mode & 0o777 != 0o600
            or (hasattr(os, "getuid") and info.st_uid != os.getuid())
            or not 0 < info.st_size <= 32768
        ):
            return None
        raw = bytearray()
        while len(raw) <= 32768:
            chunk = os.read(descriptor, min(8192, 32769 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        if len(raw) > 32768:
            return None
        from ..rag.auth import strict_json

        value = strict_json(bytes(raw))
        return value if type(value) is dict else None
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _run_browser(*, node, script, origin, accounts, output, cwd):
    command = [
        str(node),
        str(Path(script).resolve()),
        origin,
        str(accounts),
        str(output),
    ]
    try:
        result = subprocess.run(
            command, cwd=cwd, env=os.environ.copy(), capture_output=True, timeout=90
        )
    except subprocess.TimeoutExpired:
        report = _private_browser_report(output)
        stage = report.get("failure_stage") if report is not None else None
        raise BrowserFailure("browser_timeout", stage) from None
    except OSError:
        raise BrowserFailure("report_unavailable") from None
    report = _private_browser_report(output)
    stage = report.get("failure_stage") if report is not None else None
    if type(result.returncode) is not int or result.returncode != 0:
        raise BrowserFailure("browser_nonzero", stage, result.returncode)
    if (
        report is None
        or report.get("passed") is not True
        or type(report.get("schema_version")) is not int
        or report["schema_version"] != 1
    ):
        raise BrowserFailure("report_unavailable", stage, result.returncode)
    return report


class Form(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.action = self.csrf = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "form":
            self.action = fields.get("action")
        if tag == "input" and fields.get("name") == "csrf":
            self.csrf = fields.get("value")


class AdminIssuer:
    def __init__(self, directory, node, redirect_uri, accounts):
        self.process = _JsonProcess(
            [
                str(node),
                str(directory / "dist/admin-bridge.js"),
                "--redirect-uri",
                redirect_uri,
                "--accounts-file",
                str(accounts),
            ],
            cwd=directory,
            env=os.environ.copy(),
            timeout=15,
        )
        try:
            value = self.process.read()
            if (
                value.get("event") != "ready"
                or value.get("audience") != "urn:identity-boundary:admin"
                or value.get("client_id") != "admin-portal"
            ):
                raise IntegrationError(
                    "Dedicated identity fixture did not become ready."
                )
            self.issuer = local_origin(value["issuer"])
        except BaseException:
            self.close()
            raise

    def close(self):
        self.process.close(eof=True)


FAULT_SERVER = """
import json,os,socket,sys,time
from pathlib import Path
import uvicorn
from enterprise_ai_lab.admin.api import create_app
from enterprise_ai_lab.admin.auth import AdminTokenVerifier
from enterprise_ai_lab.admin.core import AdminStore
from enterprise_ai_lab.mcp_gateway.store import ReadOnlyStore
issuer,source,database,port,marker,phase=sys.argv[1:]
if phase not in ('before_commit','after_commit'):raise ValueError('fixed phase required')
def fault(current):
    if current!=phase:return
    pending=marker+'.pending'
    fd=os.open(pending,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(json.dumps({'phase':phase}));stream.flush();os.fsync(stream.fileno())
    os.replace(pending,marker)
    until=time.monotonic()+8
    while time.monotonic()<until:time.sleep(.01)
    raise RuntimeError('controlled local barrier expired')
store=ReadOnlyStore(source)
service=AdminStore(database,store,fault_hook=fault)
sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
sock.bind(('127.0.0.1',int(port)));sock.listen(32)
origin='http://127.0.0.1:'+str(sock.getsockname()[1])
app=create_app(origin=origin,verifier=AdminTokenVerifier.discover(issuer),service=service)
print(json.dumps({'event':'ready','url':origin}),flush=True)
try:uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='critical')).run(sockets=[sock])
finally:service.close();store.close();sock.close()
"""


class Portal:
    def __init__(self, *, issuer, source, database, port, marker=None, phase=None):
        import_path = Path(__file__).resolve().parents[2]
        if phase is None:
            command = [
                sys.executable,
                "-m",
                "enterprise_ai_lab.admin",
                "serve",
                "--issuer",
                issuer,
                "--source",
                str(source),
                "--database",
                str(database),
                "--port",
                str(port),
            ]
        else:
            if phase not in ("before_commit", "after_commit") or marker is None:
                raise ValueError("fixed fault phase required")
            command = [
                sys.executable,
                "-c",
                FAULT_SERVER,
                issuer,
                str(source),
                str(database),
                str(port),
                str(marker),
                phase,
            ]
        self.process = _JsonProcess(
            command,
            cwd=Path(database).parent,
            env={**os.environ, "PYTHONPATH": str(import_path)},
            timeout=15,
        )
        try:
            ready = self.process.read()
            if ready.get("event") != "ready":
                raise IntegrationError("Local admin portal did not become ready.")
            self.url = local_origin(ready["url"])
            deadline = time.monotonic() + 4
            with httpx.Client(trust_env=False, timeout=0.5) as client:
                while True:
                    try:
                        response = client.get(self.url + "/healthz")
                        if response.status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if time.monotonic() >= deadline:
                        raise IntegrationError(
                            "Local admin HTTP listener did not start."
                        )
                    time.sleep(0.02)
        except BaseException:
            self.close()
            raise

    def kill(self):
        self.process._child.kill()
        self.process.close()

    def close(self):
        self.process.close()


class BrowserProtocol:
    """Real cookie/form/code+PKCE flow; this is not a rendered-browser claim."""

    def __init__(self, origin, issuer):
        self.origin, self.issuer = origin, issuer
        self.client = httpx.Client(trust_env=False, timeout=12, follow_redirects=False)
        self.csrf = None

    def login(self, username, password):
        response = self.client.get(self.origin + "/login")
        for _ in range(10):
            if response.status_code in (302, 303, 307):
                target = urljoin(str(response.url), response.headers["location"])
                parts = urlsplit(target)
                if f"{parts.scheme}://{parts.netloc}" not in (self.origin, self.issuer):
                    raise IntegrationError("Login left the fixed loopback origins.")
                response = self.client.get(target)
                continue
            if response.status_code == 200 and "/interaction/" in str(response.url):
                form = Form(response.text)
                if not form.action or not form.csrf:
                    raise IntegrationError("Local account form missing.")
                target = urljoin(self.issuer, form.action)
                parts = urlsplit(target)
                if f"{parts.scheme}://{parts.netloc}" != self.issuer:
                    raise IntegrationError("Account form left its issuer.")
                response = self.client.post(
                    target,
                    data={
                        "csrf": form.csrf,
                        "username": username,
                        "password": password,
                    },
                    headers={"origin": self.issuer},
                )
                continue
            break
        if response.status_code != 200 or str(response.url) != self.origin + "/":
            raise IntegrationError("Actual account login did not complete.")
        response = self.call("/api/session")
        if response.status_code != 200:
            raise IntegrationError("Actual account session missing.")
        value = response.json()
        self.csrf = value["csrf_token"]
        return {
            k: value[k]
            for k in ("authenticated", "user", "can_read", "can_configure", "can_audit")
        }

    def call(self, path, *, body=None, csrf=True, origin=True):
        headers = {}
        if body is not None:
            if origin:
                headers["origin"] = self.origin
            if csrf and self.csrf:
                headers["x-csrf-token"] = self.csrf
        return self.client.request(
            "POST" if body is not None else "GET",
            self.origin + path,
            headers=headers,
            json=body,
        )

    def close(self):
        self.client.close()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def source_fingerprints():
    package = Path(__file__).resolve().parents[1]
    paths = [
        *sorted((package / "admin").glob("*.py")),
        *sorted((package / "admin/static").glob("*")),
        package / "review/sessions.py",
        package / "rag/auth.py",
        package / "rag/contracts.py",
        package / "rag/integration.py",
        package / "mcp_gateway/store.py",
        package / "mcp_gateway/data/work-orders-v1.json",
    ]
    return {
        "enterprise_ai_lab/" + str(path.relative_to(package)): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in paths
    }


def identity_fingerprints(identity):
    return {
        name: hashlib.sha256((Path(identity) / name).read_bytes()).hexdigest()
        for name in (
            "dist/admin-bridge.js",
            "dist/admin-issuer.js",
            "dist/http.js",
            "package.json",
            "package-lock.json",
        )
    }


def run_demo(args):
    work, out = Path(args.work_dir).absolute(), Path(args.out).absolute()
    if (
        work.is_symlink()
        or out.exists()
        or out.is_symlink()
        or (work.exists() and any(work.iterdir()))
    ):
        raise ValueError("fresh work and report paths required")
    work.mkdir(parents=True, mode=0o700, exist_ok=True)
    work.chmod(0o700)
    identity = Path(args.identity_dir).resolve(strict=True)
    browser_script = getattr(args, "browser_script", None)
    if getattr(args, "browser_test", False) and (
        browser_script is None or not Path(browser_script).is_file()
    ):
        raise ValueError("explicit browser script required")
    if not (identity / "dist/admin-bridge.js").is_file():
        raise ValueError("built dedicated admin fixture required")
    before_sources, before_identity = (
        source_fingerprints(),
        identity_fingerprints(identity),
    )
    source, database = work / "source.sqlite3", work / "admin.sqlite3"
    fixture = initialize_fixture(source)
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    names = (
        "admin-alpha",
        "admin-beta",
        "auditor-alpha",
        "auditor-beta",
        "observer-alpha",
    )
    passwords = {name: secrets.token_urlsafe(24) for name in names}
    accounts = work / "accounts.private.json"
    fd = os.open(accounts, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(
            {
                "schema_version": 1,
                "accounts": [
                    {"username": name, "password": password}
                    for name, password in passwords.items()
                ],
            },
            stream,
        )
    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    issuer = portal = None
    processes, clients, observations, faults = [], [], [], []

    def require(condition):
        if not condition:
            raise IntegrationError("An actual admin experiment assertion failed.")

    def start(phase=None):
        value = Portal(
            issuer=issuer.issuer,
            source=source,
            database=database,
            port=port,
            phase=phase,
            marker=work / (phase + ".ready") if phase else None,
        )
        processes.append(value.process)
        return value

    def client(name):
        value = BrowserProtocol(origin, issuer.issuer)
        clients.append(value)
        login = value.login(name, passwords[name])
        require(login["user"]["subject"] == name)
        observations.append(
            {"case": "login-" + name, "http_status": 200, "response": login}
        )
        return value

    def observe(browser, case, path, *, expected=200, **kwargs):
        response = browser.call(path, **kwargs)
        require(response.status_code == expected)
        require(response.headers.get("cache-control") == "no-store")
        body = response.json()
        observations.append(
            {"case": case, "http_status": response.status_code, "response": body}
        )
        return body

    def update(identifier, revision, size, status="open"):
        return {
            "command_id": identifier,
            "expected_revision": revision,
            "action": "update",
            "config": {"work_order_page_size": size, "default_status": status},
        }

    try:
        issuer = AdminIssuer(identity, args.node, origin + "/auth/callback", accounts)
        processes.append(issuer.process)
        portal = start()
        anonymous = BrowserProtocol(origin, issuer.issuer)
        clients.append(anonymous)
        observe(anonymous, "unauthenticated", "/api/config", expected=401)
        alpha, beta, auditor, observer = (
            client(name)
            for name in ("admin-alpha", "admin-beta", "auditor-alpha", "observer-alpha")
        )
        observe(observer, "observer-denied", "/api/config", expected=403)
        before = observe(alpha, "alpha-initial-config", "/api/config")
        require(before["revision"] == 0)
        initial_orders = observe(alpha, "initial-work-orders", "/api/work-orders")
        require(all(row["tenant_id"] == "alpha" for row in initial_orders["items"]))
        body = update("shared-command", 0, 1)
        observe(
            auditor, "auditor-write-denied", "/api/commands", body=body, expected=403
        )
        observe(
            alpha, "csrf-denied", "/api/commands", body=body, csrf=False, expected=403
        )
        observe(
            alpha,
            "origin-denied",
            "/api/commands",
            body=body,
            origin=False,
            expected=403,
        )
        first = observe(alpha, "alpha-update", "/api/commands", body=body)
        require(
            observe(alpha, "alpha-exact-replay", "/api/commands", body=body) == first
        )
        observe(
            alpha,
            "immutable-command-conflict",
            "/api/commands",
            body=update("shared-command", 0, 2),
            expected=409,
        )
        observe(
            alpha,
            "stale-revision-conflict",
            "/api/commands",
            body=update("stale-command", 0, 2),
            expected=409,
        )
        rows = observe(alpha, "configured-read", "/api/work-orders")
        require(len(rows["items"]) == 1 and rows["configuration"]["revision"] == 1)
        beta_first = observe(
            beta,
            "beta-independent-same-id",
            "/api/commands",
            body=update("shared-command", 0, 2, "closed"),
        )
        require(beta_first["tenant_id"] == "beta" and beta_first["revision"] == 1)
        beta_rows = observe(beta, "beta-filtered-read", "/api/work-orders")
        require(all(row["tenant_id"] == "beta" for row in beta_rows["items"]))
        rollback = {
            "command_id": "restore-zero",
            "expected_revision": 1,
            "action": "rollback",
            "target_revision": 0,
        }
        restored = observe(
            alpha, "rollback-new-revision", "/api/commands", body=rollback
        )
        require(restored["revision"] == 2)
        current = observe(alpha, "rollback-restored-defaults", "/api/config")
        require(current["config"] == before["config"])
        audit = observe(auditor, "auditor-tenant-history", "/api/audit")
        require(
            audit["total"] == 2
            and all(row["tenant_id"] == "alpha" for row in audit["events"])
        )
        portal.close()
        portal = start()
        observe(alpha, "restart-requires-login", "/api/session", expected=401)
        alpha = client("admin-alpha")
        require(
            observe(alpha, "restart-exact-replay", "/api/commands", body=rollback)
            == restored
        )
        require(observe(alpha, "restart-audit-preserved", "/api/audit")["total"] == 2)

        for phase, command_revision, expected_before, expected_after in (
            ("before_commit", 2, 2, 3),
            ("after_commit", 3, 4, 4),
        ):
            # The second phase follows a committed before-commit retry, so its
            # accepted command creates revision4 before the response is killed.
            command = update(
                "fault-" + phase, command_revision, 4 if phase == "before_commit" else 5
            )
            portal.close()
            portal = start(phase)
            alpha = client("admin-alpha")
            marker = work / (phase + ".ready")
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(alpha.call, "/api/commands", body=command)
                deadline = time.monotonic() + 5
                while not marker.exists():
                    if future.done() or time.monotonic() >= deadline:
                        raise IntegrationError(
                            "Controlled commit barrier was not reached."
                        )
                    time.sleep(0.01)
                require(json.loads(marker.read_text()) == {"phase": phase})
                portal.kill()
                response_lost = False
                try:
                    future.result(timeout=2)
                except httpx.HTTPError:
                    response_lost = True
                require(response_lost)
            portal = start()
            observe(alpha, phase + "-old-session-denied", "/api/session", expected=401)
            alpha = client("admin-alpha")
            recovered = observe(alpha, phase + "-durable-before-retry", "/api/config")
            require(recovered["revision"] == expected_before)
            receipt = observe(
                alpha, phase + "-exact-retry", "/api/commands", body=command
            )
            require(receipt["revision"] == expected_after)
            require(
                observe(
                    alpha, phase + "-second-exact-replay", "/api/commands", body=command
                )
                == receipt
            )
            faults.append(
                {
                    "phase": phase,
                    "controlled_fault": True,
                    "response_lost": True,
                    "immutable_command_sha256": hashlib.sha256(
                        json.dumps(
                            command, sort_keys=True, separators=(",", ":")
                        ).encode()
                    ).hexdigest(),
                    "revision_after_reopen_before_retry": recovered["revision"],
                    "revision_after_explicit_retry": receipt["revision"],
                    "receipt": receipt,
                    "same_id_and_body": True,
                    "automatic_retry": False,
                }
            )
        final_audit = observe(alpha, "final-alpha-audit", "/api/audit")
        require(final_audit["total"] == 4)
        browser_result = None
        if getattr(args, "browser_test", False):
            browser_out = work / "browser.json"
            browser_result = _run_browser(
                node=args.node,
                script=browser_script,
                origin=origin,
                accounts=accounts,
                output=browser_out,
                cwd=work,
            )
        observe(alpha, "logout", "/logout", body={})
        observe(alpha, "logged-out-denied", "/api/config", expected=401)
        require(hashlib.sha256(source.read_bytes()).hexdigest() == source_hash)
        require(
            source_fingerprints() == before_sources
            and identity_fingerprints(identity) == before_identity
        )
    finally:
        for value in clients:
            value.close()
        if portal is not None:
            portal.close()
        if issuer is not None:
            issuer.close()
        passwords.clear()

    report = {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "experiment": "Fictional local configuration portal with actual OIDC, HTTP and SQLite",
        "data": "Packaged authored synthetic18-work-order fixture; no company system or provider calls",
        "runtime": {
            "python": platform.python_version(),
            "sqlite": __import__("sqlite3").sqlite_version,
            "package": version("enterprise-ai-lab"),
        },
        "oidc": {
            "flow": "authorization_code",
            "pkce": "S256",
            "audience": "urn:identity-boundary:admin",
            "client_id": "admin-portal",
        },
        "fixture": fixture,
        "source_database_sha256_before_and_after": source_hash,
        "source_sha256": before_sources,
        "identity_dist_sha256": before_identity,
        "browser_script_sha256": hashlib.sha256(
            Path(browser_script).read_bytes()
        ).hexdigest()
        if browser_script is not None and getattr(args, "browser_test", False)
        else None,
        "observations": observations,
        "faults": faults,
        "browser": browser_result,
        "processes": {
            "count": len(processes),
            "all_reaped": all(p._child.poll() is not None for p in processes),
            "scope": "issuer-and-portal-service-children",
        },
        "human_review_performed": False,
        "registry_release": False,
        "limitations": [
            "Generated fixture accounts and scripted browser actions do not establish owner study or human review.",
            "Trusted loopback host/filesystem, startup keys and short-lived sessions are not production identity, MFA, key rotation or live entitlement synchronization.",
            "Only two fictional view settings change. No IAM, permission, process, service or arbitrary-command operation is exposed.",
            "Fault hooks deliberately pause before or after the actual SQLite commit; they do not identify a historical failure cause.",
            "The local mutation chain shares the configuration transaction but has no external anchor against the trusted host.",
            "A lost HTTP response is not rollback; one explicit identical retry reconciles a durable receipt. No exactly-once network delivery claim.",
        ],
    }
    if not report["processes"]["all_reaped"]:
        raise IntegrationError("An owned experiment child was not reaped.")
    out.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return report


def load_report(path):
    """Bounded no-follow read. Reports are data, never executable instructions."""
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 1_000_000:
            raise ValueError("bounded regular report required")
        raw = bytearray()
        while len(raw) <= 1_000_000:
            value = os.read(descriptor, min(65536, 1_000_001 - len(raw)))
            if not value:
                break
            raw.extend(value)
        if len(raw) > 1_000_000:
            raise ValueError("report exceeds bound")
        from ..rag.auth import strict_json

        return strict_json(bytes(raw))
    finally:
        os.close(descriptor)


def verify_report(report, *, identity_dir):
    """Recompute fixed fictional receipts and current source fingerprints offline.

    This checks internal evidence consistency, not signatures, actual execution,
    externally anchored audit, production readiness or independent human review.
    Historical reports require their recorded source checkout, not current code.
    """

    def require(value):
        if value is not True:
            raise ValueError("admin report failed bounded consistency verification")

    def canonical(value):
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    def digest(value):
        return hashlib.sha256(canonical(value).encode()).hexdigest()

    require(type(report) is dict)
    require(
        set(report)
        == {
            "schema_version",
            "recorded_at",
            "experiment",
            "data",
            "runtime",
            "oidc",
            "fixture",
            "source_database_sha256_before_and_after",
            "source_sha256",
            "identity_dist_sha256",
            "browser_script_sha256",
            "observations",
            "faults",
            "browser",
            "processes",
            "human_review_performed",
            "registry_release",
            "limitations",
        }
    )
    require(type(report["schema_version"]) is int and report["schema_version"] == 1)
    require(
        report["human_review_performed"] is False
        and report["registry_release"] is False
    )
    require(
        report["processes"]
        == {
            "count": 7,
            "all_reaped": True,
            "scope": "issuer-and-portal-service-children",
        }
    )
    require(
        type(report["processes"]["count"]) is int
        and report["processes"]["all_reaped"] is True
    )
    require(
        report["oidc"]
        == {
            "flow": "authorization_code",
            "pkce": "S256",
            "audience": "urn:identity-boundary:admin",
            "client_id": "admin-portal",
        }
    )
    raw = canonical(report)
    require(len(raw.encode()) <= 1_000_000)
    import re

    require(
        re.search(
            r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b|Bearer\s+[A-Za-z0-9_.-]{20,}",
            raw,
        )
        is None
    )

    def safe(value):
        if type(value) is dict:
            require(
                not {
                    "password",
                    "access_token",
                    "id_token",
                    "csrf_token",
                    "code_verifier",
                    "session_cookie",
                }.intersection(value)
            )
            for child in value.values():
                safe(child)
        elif type(value) is list:
            for child in value:
                safe(child)
        elif type(value) is str:
            require(
                not value.startswith(("/", "file:"))
                and "/Users/" not in value
                and "\\" not in value
            )

    safe(report)
    require(report["source_sha256"] == source_fingerprints())
    require(report["identity_dist_sha256"] == identity_fingerprints(identity_dir))
    from ..mcp_gateway.store import fixture_source

    fixture, fixture_sha = fixture_source()
    require(
        report["fixture"]
        == {
            "schema_version": 1,
            "fixture_sha256": fixture_sha,
            "work_order_count": len(fixture["work_orders"]),
            "diagnostic_count": len(fixture["asset_diagnostics"]),
        }
    )
    require(
        type(report["source_database_sha256_before_and_after"]) is str
        and bool(
            re.fullmatch(
                "[a-f0-9]{64}", report["source_database_sha256_before_and_after"]
            )
        )
    )
    observations = report["observations"]
    require(type(observations) is list and len(observations) == 40)
    expected = [
        ("unauthenticated", 401),
        ("login-admin-alpha", 200),
        ("login-admin-beta", 200),
        ("login-auditor-alpha", 200),
        ("login-observer-alpha", 200),
        ("observer-denied", 403),
        ("alpha-initial-config", 200),
        ("initial-work-orders", 200),
        ("auditor-write-denied", 403),
        ("csrf-denied", 403),
        ("origin-denied", 403),
        ("alpha-update", 200),
        ("alpha-exact-replay", 200),
        ("immutable-command-conflict", 409),
        ("stale-revision-conflict", 409),
        ("configured-read", 200),
        ("beta-independent-same-id", 200),
        ("beta-filtered-read", 200),
        ("rollback-new-revision", 200),
        ("rollback-restored-defaults", 200),
        ("auditor-tenant-history", 200),
        ("restart-requires-login", 401),
        ("login-admin-alpha", 200),
        ("restart-exact-replay", 200),
        ("restart-audit-preserved", 200),
        ("login-admin-alpha", 200),
        ("before_commit-old-session-denied", 401),
        ("login-admin-alpha", 200),
        ("before_commit-durable-before-retry", 200),
        ("before_commit-exact-retry", 200),
        ("before_commit-second-exact-replay", 200),
        ("login-admin-alpha", 200),
        ("after_commit-old-session-denied", 401),
        ("login-admin-alpha", 200),
        ("after_commit-durable-before-retry", 200),
        ("after_commit-exact-retry", 200),
        ("after_commit-second-exact-replay", 200),
        ("final-alpha-audit", 200),
        ("logout", 200),
        ("logged-out-denied", 401),
    ]
    require(
        all(
            type(row) is dict
            and set(row) == {"case", "http_status", "response"}
            and type(row["http_status"]) is int
            for row in observations
        )
    )
    require([(row["case"], row["http_status"]) for row in observations] == expected)
    cases = {row["case"]: row["response"] for row in observations}
    commands = [
        (
            "alpha-update",
            "alpha",
            "admin-alpha",
            {
                "command_id": "shared-command",
                "expected_revision": 0,
                "action": "update",
                "config": {"work_order_page_size": 1, "default_status": "open"},
            },
            1,
        ),
        (
            "beta-independent-same-id",
            "beta",
            "admin-beta",
            {
                "command_id": "shared-command",
                "expected_revision": 0,
                "action": "update",
                "config": {"work_order_page_size": 2, "default_status": "closed"},
            },
            1,
        ),
        (
            "rollback-new-revision",
            "alpha",
            "admin-alpha",
            {
                "command_id": "restore-zero",
                "expected_revision": 1,
                "action": "rollback",
                "target_revision": 0,
            },
            2,
        ),
        (
            "before_commit-exact-retry",
            "alpha",
            "admin-alpha",
            {
                "command_id": "fault-before_commit",
                "expected_revision": 2,
                "action": "update",
                "config": {"work_order_page_size": 4, "default_status": "open"},
            },
            3,
        ),
        (
            "after_commit-exact-retry",
            "alpha",
            "admin-alpha",
            {
                "command_id": "fault-after_commit",
                "expected_revision": 3,
                "action": "update",
                "config": {"work_order_page_size": 5, "default_status": "open"},
            },
            4,
        ),
    ]
    events, previous = [], "0" * 64
    for sequence, (case, tenant, subject, command, revision) in enumerate(commands, 1):
        config = command.get(
            "config", {"work_order_page_size": 3, "default_status": "open"}
        )
        event = {
            "sequence": sequence,
            "tenant_id": tenant,
            "subject": subject,
            "command_id": command["command_id"],
            "action": command["action"],
            "command_sha256": digest(
                {
                    "tenant_id": tenant,
                    "subject": subject,
                    "client_id": "admin-portal",
                    "command": command,
                }
            ),
            "revision": revision,
            "previous_revision": revision - 1,
            "configuration_sha256": digest(config),
            "previous_audit_hash": previous,
        }
        previous = digest(event)
        receipt = {
            key: event[key]
            for key in (
                "tenant_id",
                "command_id",
                "action",
                "revision",
                "previous_revision",
                "configuration_sha256",
            )
        }
        receipt.update(audit_sequence=sequence, audit_hash=previous)
        require(canonical(cases[case]) == canonical(receipt))
        events.append(event | {"audit_hash": previous})
    require(canonical(cases["alpha-exact-replay"]) == canonical(cases["alpha-update"]))
    require(
        canonical(cases["restart-exact-replay"])
        == canonical(cases["rollback-new-revision"])
    )
    for phase in ("before_commit", "after_commit"):
        require(cases[phase + "-second-exact-replay"] == cases[phase + "-exact-retry"])
    require(
        cases["final-alpha-audit"]
        == {
            "tenant_id": "alpha",
            "events": [events[index] for index in (0, 2, 3, 4)],
            "total": 4,
        }
    )
    faults = report["faults"]
    require(type(faults) is list and len(faults) == 2)
    for index, (phase, before, after) in enumerate(
        (("before_commit", 2, 3), ("after_commit", 4, 4))
    ):
        command = commands[index + 3][3]
        require(
            canonical(faults[index])
            == canonical(
                {
                    "phase": phase,
                    "controlled_fault": True,
                    "response_lost": True,
                    "immutable_command_sha256": digest(command),
                    "revision_after_reopen_before_retry": before,
                    "revision_after_explicit_retry": after,
                    "receipt": cases[phase + "-exact-retry"],
                    "same_id_and_body": True,
                    "automatic_retry": False,
                }
            )
        )
        require(cases[phase + "-durable-before-retry"]["revision"] == before)
    if report["browser"] is None:
        require(report["browser_script_sha256"] is None)
    else:
        require(
            report["browser"]["passed"] is True
            and report["browser"]["human_review_performed"] is False
        )
        require(
            type(report["browser"]["checks"]) is list
            and len(report["browser"]["checks"]) == 9
            and all(row.get("passed") is True for row in report["browser"]["checks"])
        )
        require(
            type(report["browser_script_sha256"]) is str
            and bool(re.fullmatch("[a-f0-9]{64}", report["browser_script_sha256"]))
        )
    return {
        "verified": True,
        "observations": 40,
        "recomputed_committed_receipts": 5,
        "controlled_commit_faults": 2,
        "current_python_and_identity_sources_verified": True,
        "execution_or_human_review_proved_by_verifier": False,
    }
