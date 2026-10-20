"""Actual FastAPI/PyJWT/SQLite boundary, controlled RSA keys and local doubles.

Processor doubles below are deliberately not neural inference or real HTTP
processor evidence. The separate process demo exercises that integration.
All text and accounts in this file are authored synthetic fixtures.
"""
from contextlib import ExitStack, closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import sqlite3
import threading
import time
from types import SimpleNamespace

from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
import jwt
import pytest

from enterprise_ai_lab.privacy_proxy.api import MAX_BODY, create_app
from enterprise_ai_lab.privacy_proxy.vault import Vault
from enterprise_ai_lab.rag import auth


TEXT = "🙂 Contact alice@example.invalid or +1 416 555 0123."
SECRETS = ("alice@example.invalid", "+1 416 555 0123")
ISSUER = "http://127.0.0.1:8181"


class ControlledProcessor:
    """Explicit local double: observes only redacted input, applies one callback."""
    def __init__(self, callback=lambda text: "Processed: " + text):
        self.callback, self.inputs = callback, []

    def process(self, text):
        self.inputs.append(text)
        return self.callback(text)


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def boundary(tmp_path, signing_key):
    with ExitStack() as stack:
        def make(*, processor=None, clock=None):
            private = tmp_path / f"private-{len(list(tmp_path.iterdir()))}"
            private.mkdir(mode=0o700)
            path = private / "vault.sqlite"
            vault = stack.enter_context(Vault(path, **({"clock": clock} if clock else {})))
            public = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
            public.update(kid="privacy-api-controlled-key", alg="RS256", use="sig")
            verifier = auth.JwtVerifier(ISSUER, {"keys": [public]}, audience=auth.PRIVACY_AUDIENCE)
            client = stack.enter_context(TestClient(create_app(verifier, vault, processor)))

            def token(*, tenant="alpha", client_id="privacy-alpha", roles=("redactor", "restorer"),
                      scopes="privacy:redact privacy:restore", now=None, **changes):
                now = int(time.time()) if now is None else now
                claims = dict(iss=ISSUER, aud=auth.PRIVACY_AUDIENCE, sub=client_id,
                              client_id=client_id, tenant_id=tenant, roles=list(roles),
                              scope=scopes, iat=now, exp=now + 60, jti="controlled-api-jti")
                claims.update(changes)
                return jwt.encode(claims, signing_key, algorithm="RS256",
                                  headers={"kid": "privacy-api-controlled-key", "typ": "at+jwt"})

            return SimpleNamespace(client=client, vault=vault, path=path, verifier=verifier,
                                   token=token, processor=processor)
        yield make


def endpoint(operation="redactions", tenant="alpha"):
    return f"/v1/tenants/{tenant}/privacy/{operation}"


def post(harness, *, operation="redactions", tenant="alpha", token=None, body=None, **kwargs):
    headers = {"Authorization": "Bearer " + (token or harness.token())}
    return harness.client.post(endpoint(operation, tenant),
                               json={"text": TEXT} if body is None else body,
                               headers=headers, **kwargs)


def counts(harness):
    with closing(sqlite3.connect(harness.path)) as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("contexts", "mappings", "ids"))


def assert_safe_error(response, status, code, *, submitted=TEXT, token="", forbidden=()):
    assert response.status_code == status
    value = response.json()
    assert set(value) == {"error"} and set(value["error"]) == {"code", "message"}
    assert value["error"]["code"] == code
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for item in (submitted, token, *SECRETS, *forbidden):
        if item:
            assert item not in response.text
    if status == 401:
        assert response.headers["www-authenticate"].startswith('Bearer realm="privacy-proxy"')


def test_dedicated_verifier_required_and_health_has_no_store(boundary):
    h = boundary()
    assert h.client.get("/healthz").headers["cache-control"] == "no-store"
    h.verifier.audience = auth.RAG_AUDIENCE
    with pytest.raises(ValueError, match="dedicated privacy verifier"):
        create_app(h.verifier, h.vault)


def test_actual_signed_principal_redacts_then_restores_only_for_same_tenant_client_context(boundary):
    h = boundary()
    bearer = h.token()
    redacted = post(h, token=bearer)
    assert redacted.status_code == 200
    assert redacted.headers["cache-control"] == "no-store"
    value = redacted.json()
    assert set(value) == {"context_id", "redacted_text", "expires_at", "detections"}
    assert len(value["detections"]) == 2
    assert {d["kind"] for d in value["detections"]} == {"EMAIL", "PHONE"}
    assert bearer not in redacted.text
    for secret in SECRETS:
        assert secret not in redacted.text
    for detection in value["detections"]:
        assert TEXT[detection["start"]:detection["end"]] in SECRETS
        assert detection["token"] in value["redacted_text"]
    body = {"context_id": value["context_id"], "text": value["redacted_text"]}
    restored = post(h, operation="restorations", token=bearer, body=body)
    assert restored.status_code == 200 and restored.json() == {"restored_text": TEXT}
    assert restored.headers["cache-control"] == "no-store"
    assert bearer not in restored.text


@pytest.mark.parametrize("operation", ["redactions", "restorations", "process"])
@pytest.mark.parametrize("roles,scopes", [
    (("reader",), "privacy:redact privacy:restore"),
    (("redactor", "restorer"), "rag:query"),
])
def test_verified_scope_and_role_are_both_required(boundary, operation, roles, scopes):
    h = boundary(processor=ControlledProcessor())
    bearer = h.token(roles=roles, scopes=scopes)
    body = {"text": TEXT, **({"context_id": "0" * 32} if operation == "restorations" else {})}
    assert_safe_error(post(h, operation=operation, token=bearer, body=body), 403, "forbidden", token=bearer)
    assert counts(h) == (0, 0, 0)
    assert h.processor.inputs == []


@pytest.mark.parametrize("roles,scopes,expected", [
    (("redactor",), "privacy:redact", 200),
    (("restorer",), "privacy:redact", 403),
    (("redactor",), "privacy:restore", 403),
])
def test_redactor_least_privilege_is_operation_specific(boundary, roles, scopes, expected):
    h = boundary()
    response = post(h, token=h.token(roles=roles, scopes=scopes))
    assert response.status_code == expected


def test_restorer_only_can_restore_its_context_but_cannot_redact(boundary):
    h = boundary()
    value = post(h).json()
    bearer = h.token(roles=("restorer",), scopes="privacy:restore")
    body = {"context_id": value["context_id"], "text": value["redacted_text"]}
    response = post(h, operation="restorations", token=bearer, body=body)
    assert response.status_code == 200 and response.json() == {"restored_text": TEXT}
    assert_safe_error(post(h, token=bearer), 403, "forbidden", token=bearer)


@pytest.mark.parametrize("roles,scopes", [
    (("redactor",), "privacy:redact privacy:restore"),
    (("restorer",), "privacy:redact privacy:restore"),
    (("redactor", "restorer"), "privacy:redact"),
    (("redactor", "restorer"), "privacy:restore"),
])
def test_process_requires_both_roles_and_both_scopes_before_outbound_work(boundary, roles, scopes):
    processor = ControlledProcessor()
    h = boundary(processor=processor)
    assert_safe_error(post(h, operation="process", token=h.token(roles=roles, scopes=scopes)), 403, "forbidden")
    assert processor.inputs == [] and counts(h) == (0, 0, 0)


@pytest.mark.parametrize("tenant", ["beta", "Alpha", "a" * 65])
def test_url_tenant_never_overrides_signed_tenant(boundary, tenant):
    h = boundary()
    assert_safe_error(post(h, tenant=tenant), 403, "forbidden")
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("kind", ["missing", "malformed", "duplicate", "wrong_audience", "tampered", "expired"])
def test_missing_duplicate_or_invalid_authorization_never_creates_context(boundary, kind):
    h = boundary()
    bearer = h.token()
    headers = {"Authorization": "Bearer " + bearer}
    if kind == "missing":
        headers = {}
    elif kind == "malformed":
        headers = {"Authorization": "Basic private-credential"}
    elif kind == "duplicate":
        headers = [("Authorization", "Bearer " + bearer), ("authorization", "Bearer " + bearer)]
    elif kind == "wrong_audience":
        bearer = h.token(aud=auth.RAG_AUDIENCE)
        headers = {"Authorization": "Bearer " + bearer}
    elif kind == "tampered":
        parts = bearer.split(".")
        parts[2] = ("A" if parts[2][0] != "A" else "B") + parts[2][1:]
        bearer = ".".join(parts)
        headers = {"Authorization": "Bearer " + bearer}
    elif kind == "expired":
        now = int(time.time())
        bearer = h.token(now=now - 61, exp=now - 1)
        headers = {"Authorization": "Bearer " + bearer}
    response = h.client.post(endpoint(), json={"text": TEXT}, headers=headers)
    assert_safe_error(response, 401, "invalid_token", token=bearer, forbidden=("private-credential",))
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("header", ["X-Tenant-Id", "X-Roles", "X-Role", "X-User-Id", "X-Client-Id"])
def test_caller_identity_headers_are_rejected_even_with_valid_bearer(boundary, header):
    h = boundary()
    bearer = h.token()
    response = h.client.post(endpoint(), json={"text": TEXT},
                             headers={"Authorization": "Bearer " + bearer, header: "private-spoof"})
    assert_safe_error(response, 400, "invalid_request", token=bearer, forbidden=("private-spoof",))
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("query", ["tenant_id=beta", "roles=restorer", "client_id=other", "unused=1"])
def test_all_query_overrides_are_rejected(boundary, query):
    h = boundary()
    response = h.client.post(endpoint() + "?" + query, json={"text": TEXT},
                             headers={"Authorization": "Bearer " + h.token()})
    assert_safe_error(response, 400, "invalid_request")
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("extra", ["tenant_id", "client_id", "roles", "scope", "context_id", "processor_url"])
def test_redaction_body_cannot_supply_identity_or_processor_configuration(boundary, extra):
    h = boundary()
    assert_safe_error(post(h, body={"text": TEXT, extra: "private-extra"}), 400, "invalid_request",
                      forbidden=("private-extra",))
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("raw", [
    b'{"text":"first","text":"private-duplicate"}',
    b'{"text":NaN}', b'{"text":1e999}', b'{"text":"\xff"}',
    b'{"text":"\\ud800"}', b'[]', b'null', b'{',
    b'{"text":' + b'[' * 10 + b'0' + b']' * 10 + b'}',
])
def test_strict_json_rejects_duplicates_nonfinite_invalid_utf8_surrogates_and_depth(boundary, raw):
    h = boundary()
    bearer = h.token()
    response = h.client.post(endpoint(), content=raw,
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + bearer})
    assert_safe_error(response, 400, "invalid_json", token=bearer, forbidden=("private-duplicate",))
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("value", [None, True, 1, 1.5, [], {}])
def test_text_requires_exact_json_string(boundary, value):
    h = boundary()
    assert_safe_error(post(h, body={"text": value}), 400, "invalid_request")
    assert counts(h) == (0, 0, 0)


@pytest.mark.parametrize("headers", [[], [("Content-Type", "text/plain")],
                                    [("Content-Type", "application/json"), ("content-type", "application/json")]])
def test_content_type_is_single_json_value(boundary, headers):
    h = boundary()
    response = h.client.post(endpoint(), content=b'{"text":"private-text"}',
                             headers=headers + [("Authorization", "Bearer " + h.token())])
    assert_safe_error(response, 415, "unsupported_media_type", forbidden=("private-text",))
    assert counts(h) == (0, 0, 0)


def test_body_and_decoded_text_have_distinct_utf8_byte_limits(boundary):
    h = boundary()
    bearer = h.token()
    response = h.client.post(endpoint(), content=b'{"text":"' + b'x' * MAX_BODY + b'"}',
                             headers={"Content-Type": "application/json", "Authorization": "Bearer " + bearer})
    assert_safe_error(response, 413, "body_too_large", token=bearer)
    assert_safe_error(post(h, body={"text": "é" * 16385}), 400, "invalid_input")
    assert counts(h) == (0, 0, 0)
    permitted = post(h, body={"text": "é" * 16384})
    assert permitted.status_code == 200 and permitted.json()["detections"] == []


@pytest.mark.parametrize("reserved", ["[[PII:bad]]", "prefix [[PII:", "[[PII:" + "a" * 32 + "]]"])
def test_submitted_reserved_placeholders_are_rejected_before_new_mapping(boundary, reserved):
    h = boundary()
    assert_safe_error(post(h, body={"text": TEXT + reserved}), 400, "invalid_input", forbidden=(reserved,))
    assert counts(h) == (0, 0, 0)


def test_tenant_and_client_isolation_indistinguishable_from_unknown_context(boundary):
    h = boundary()
    value = post(h).json()
    body = {"context_id": value["context_id"], "text": value["redacted_text"]}
    requests = [
        ("beta", h.token(tenant="beta", client_id="privacy-beta"), body),
        ("alpha", h.token(client_id="privacy-other-alpha"), body),
        ("alpha", h.token(), {**body, "context_id": "0" * 32}),
    ]
    results = []
    for tenant, bearer, request in requests:
        response = post(h, operation="restorations", tenant=tenant, token=bearer, body=request)
        assert_safe_error(response, 404, "context_unavailable", token=bearer,
                          forbidden=(value["context_id"], value["redacted_text"]))
        results.append(response.json())
    assert results[0] == results[1] == results[2]
    assert counts(h) == (1, 2, 3)
    assert post(h, operation="restorations", body=body).json() == {"restored_text": TEXT}


@pytest.mark.parametrize("context", [None, 1, "a" * 31, "A" * 32, "0" * 32 + "\n"])
def test_context_reference_requires_exact_lowercase_identifier(boundary, context):
    h = boundary()
    response = post(h, operation="restorations", body={"text": TEXT, "context_id": context})
    assert_safe_error(response, 400, "invalid_request")
    assert counts(h) == (0, 0, 0)


def test_restore_and_process_reject_unknown_identity_fields_without_mutation(boundary):
    h = boundary(processor=ControlledProcessor())
    value = post(h).json()
    before = counts(h)
    response = post(h, operation="restorations", body={"text": value["redacted_text"],
                    "context_id": value["context_id"], "client_id": "privacy-other-alpha"})
    assert_safe_error(response, 400, "invalid_request")
    assert_safe_error(post(h, operation="process", body={"text": TEXT, "roles": ["restorer"]}),
                      400, "invalid_request")
    assert h.processor.inputs == [] and counts(h) == before


def test_invalid_cross_context_and_tampered_restoration_is_atomic_and_retryable(boundary):
    h = boundary()
    a = post(h).json()
    b = post(h, body={"text": "second@example.invalid"}).json()
    before = counts(h)
    for bad in [b["redacted_text"], "[[PII:" + "0" * 32 + "]]", "[[PII:bad]]", "[[PII:"]:
        response = post(h, operation="restorations", body={
            "context_id": a["context_id"], "text": a["redacted_text"] + " " + bad})
        assert_safe_error(response, 400, "invalid_token_reference", forbidden=("second@example.invalid",))
        assert counts(h) == before
        assert h.vault.audit_events()[-1]["outcome"] == "denied"
    valid = post(h, operation="restorations", body={"context_id": a["context_id"], "text": a["redacted_text"]})
    assert valid.json() == {"restored_text": TEXT}


def test_context_expiry_prevents_restoration_without_echoing_submitted_value(boundary):
    clock = [1000]
    h = boundary(clock=lambda: clock[0])
    value = post(h).json()
    clock[0] = value["expires_at"]
    response = post(h, operation="restorations", body={
        "context_id": value["context_id"], "text": value["redacted_text"]})
    assert_safe_error(response, 404, "context_unavailable", forbidden=(value["context_id"],))


def test_controlled_processor_observes_only_tokens_then_authorized_restore_returns_text(boundary, caplog):
    processor = ControlledProcessor()
    h = boundary(processor=processor)
    bearer = h.token()
    response = post(h, operation="process", token=bearer)
    assert response.status_code == 200
    value = response.json()
    assert value["restored_text"] == "Processed: " + TEXT
    assert processor.inputs == [value["redacted_text"]]
    assert value["processed_text"] == "Processed: " + value["redacted_text"]
    assert response.headers["cache-control"] == "no-store"
    assert bearer not in response.text and bearer not in caplog.text
    for secret in SECRETS:
        assert secret not in processor.inputs[0] and secret not in caplog.text


def test_missing_processor_never_creates_context_or_releases_text(boundary):
    h = boundary()
    assert_safe_error(post(h, operation="process"), 502, "processor_unavailable")
    assert counts(h) == (0, 0, 0)


def test_processor_exception_is_finite_no_plaintext_or_jwt_reflection(boundary, caplog):
    holder = {}
    def fail(_text):
        raise RuntimeError("PRIVATE raw exception " + TEXT + holder["token"])
    h = boundary(processor=ControlledProcessor(fail))
    holder["token"] = h.token()
    response = post(h, operation="process", token=holder["token"])
    assert_safe_error(response, 502, "processor_unavailable", token=holder["token"], forbidden=("PRIVATE", "RuntimeError"))
    assert holder["token"] not in caplog.text and TEXT not in caplog.text
    # Redaction committed before outbound processing. No restoration committed.
    assert [e["action"] for e in h.vault.audit_events()] == ["redact"]


@pytest.mark.parametrize("bad", ["[[PII:bad]]", "[[PII:" + "0" * 32 + "]]"])
def test_processor_cannot_smuggle_unknown_placeholders_past_atomic_restore(boundary, bad):
    h = boundary(processor=ControlledProcessor(lambda text: text + bad))
    response = post(h, operation="process")
    assert_safe_error(response, 400, "invalid_token_reference")
    assert h.vault.audit_events()[-1] == {"sequence": 2, "action": "restore", "outcome": "denied", "count": 0}


def test_processing_rechecks_actual_jwt_expiry_before_plaintext_release(boundary, monkeypatch):
    now = [int(time.time())]
    class ControlledDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.fromtimestamp(now[0], tz=tz or timezone.utc)
    monkeypatch.setattr(jwt.api_jwt, "datetime", ControlledDatetime)
    monkeypatch.setattr(auth, "time", SimpleNamespace(time=lambda: now[0]))
    def expire(text):
        now[0] += 2
        return text
    h = boundary(processor=ControlledProcessor(expire))
    bearer = h.token(now=now[0], exp=now[0] + 1)
    response = post(h, operation="process", token=bearer)
    assert len(h.processor.inputs) == 1
    assert_safe_error(response, 401, "invalid_token", token=bearer)
    assert [e["action"] for e in h.vault.audit_events()] == ["redact"]


def test_closed_vault_returns_finite_unavailable_response(boundary, caplog):
    h = boundary()
    h.vault.close()
    response = post(h)
    assert_safe_error(response, 503, "storage_unavailable", forbidden=(str(h.path),))
    assert TEXT not in caplog.text


def test_actual_sqlite_fault_rolls_back_context_without_exception_reflection(boundary, caplog):
    h = boundary()
    with closing(sqlite3.connect(h.path)) as connection:
        connection.execute("CREATE TRIGGER api_fault BEFORE INSERT ON mappings WHEN NEW.kind='PHONE' "
                           "BEGIN SELECT RAISE(ABORT,'PRIVATE synthetic database fault'); END")
    bearer = h.token()
    response = post(h, token=bearer)
    assert_safe_error(response, 503, "storage_unavailable", token=bearer,
                      forbidden=("PRIVATE", "database fault", str(h.path)))
    assert counts(h) == (0, 0, 0)
    assert "PRIVATE" not in caplog.text and TEXT not in caplog.text and bearer not in caplog.text
    with closing(sqlite3.connect(h.path)) as connection:
        connection.execute("DROP TRIGGER api_fault")
    # Recovery is an explicit caller retry after removing the controlled fault.
    assert post(h).status_code == 200 and counts(h) == (1, 2, 3)


def test_admission_denies_concurrent_request_before_new_context_then_recovers(boundary):
    entered, release = threading.Event(), threading.Event()
    def pause(text):
        entered.set()
        if not release.wait(timeout=3):
            raise RuntimeError("controlled processor barrier not released")
        return text
    h = boundary(processor=ControlledProcessor(pause))
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(post, h, operation="process")
        try:
            assert entered.wait(timeout=2), "real request did not reach the controlled processor"
            before = counts(h)
            assert_safe_error(post(h), 503, "resource_busy")
            assert counts(h) == before
        finally:
            release.set()
        assert pending.result(timeout=3).json()["restored_text"] == TEXT
    assert post(h).status_code == 200
