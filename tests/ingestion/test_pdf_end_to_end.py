"""Actual public PDF/OCR → MiniLM → persisted Qdrant → OAuth/JWT HTTP tests.

No document, identity, embedding, vector engine or resource response is doubled.
The two public sources receive artificial lab grants, not confidentiality claims.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from enterprise_ai_lab.rag.integration import RagSession, citation_content
from enterprise_ai_lab.rag.pdf_corpus import ingest_pdf_manifest, load_pdf_documents

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "public-data/pdf-v1"
NATIVE_QUESTION = "How is access to individual enterprise resources granted?"
OCR_QUESTION = "What functions is the Core composed of?"


def write_grants(directory: Path, *, include_beta=True) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for name in ("nist-sp-800-207.pdf", "nist-ai-100-1.pdf", "nist-ai-100-1-page25-raster.pdf"):
        shutil.copyfile(PUBLIC / name, directory / name)

    def entry(source_id, source, artifact, source_page, artifact_page, tenant, roles, uri):
        return {"source_id": source_id, "source_uri": uri,
                "source_sha256": hashlib.sha256((directory / source).read_bytes()).hexdigest(),
                "source_path": source, "artifact_path": artifact,
                "artifact_sha256": hashlib.sha256((directory / artifact).read_bytes()).hexdigest(),
                "pages": [{"artifact_page": artifact_page, "source_page": source_page}],
                "ocr_pages": [], "tenant_id": tenant, "allowed_roles": roles,
                "title": "Public NIST document — artificial lab access grant"}

    native = entry("nist-zero-trust", "nist-sp-800-207.pdf", "nist-sp-800-207.pdf", 15, 15,
                   "alpha", ["reader"],
                   "https://nvlpubs.nist.gov/nistpubs/SpecialPublications/NIST.SP.800-207.pdf")
    ocr = entry("nist-ai-rmf", "nist-ai-100-1.pdf", "nist-ai-100-1-page25-raster.pdf", 25, 1,
                "alpha", ["analyst"], "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf")
    entries = [native, ocr]
    if include_beta:
        entries.append({**copy.deepcopy(native), "tenant_id": "beta"})
    path = directory / "ingestion.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": entries}, indent=2) + "\n")
    return path


@pytest.fixture(scope="module")
def pdf_documents(tmp_path_factory):
    path = write_grants(tmp_path_factory.mktemp("public-pdf-http-fixtures"))
    return path, load_pdf_documents(path)


@pytest.fixture(scope="module")
def real_embedder():
    if os.environ.get("RUN_RAG_INTEGRATION") != "1":
        pytest.skip("Enable RUN_RAG_INTEGRATION=1 with explicit verified local model and identity paths.")
    from enterprise_ai_lab.rag.models import LocalEmbedder
    return LocalEmbedder(Path(os.environ["RAG_MODEL_DIR"]))


@pytest.fixture(scope="module")
def pdf_http(pdf_documents, real_embedder, tmp_path_factory):
    from enterprise_ai_lab.rag.store import VectorStore
    path, documents = pdf_documents
    store_path = tmp_path_factory.mktemp("pdf-http-store") / "vectors"
    store = VectorStore(store_path, real_embedder)
    store.index(documents)
    store.close()
    with RagSession(os.environ["IDENTITY_LAB_DIR"], Path(os.environ["RAG_MODEL_DIR"]), store_path,
                    node=os.environ.get("NODE_BINARY")) as session:
        yield session, {row.document_id: row for row in documents}


def ask(session, client="reader-alpha", *, tenant="alpha", question=NATIVE_QUESTION, **kwargs):
    token = session.identity.token(client)
    return session.server.query(token, tenant=tenant, query=question, **kwargs)


def verify_exact_provenance(body, by_id):
    assert body["citations"], body
    for citation in body["citations"]:
        document = by_id[citation["document_id"]]
        provenance = citation["provenance"]
        original = document.provenance
        assert document.text[provenance["start"]:provenance["end"]] == citation["text"]
        assert citation["quote"] in citation["text"]
        for key in ("source_id", "source_uri", "source_sha256", "source_page", "artifact_sha256", "artifact_page",
                    "method", "page_text_sha256"):
            assert provenance[key] == getattr(original, key)
        for word in provenance["ocr_words"]:
            assert document.text[word["start"]:word["end"]] == word["text"]
            assert 0 <= word["left"] < word["left"] + word["width"] <= provenance["width_px"]
            assert 0 <= word["top"] < word["top"] + word["height"] <= provenance["height_px"]


def test_public_native_pdf_has_exact_http_citation_and_reader_grant(pdf_http):
    session, by_id = pdf_http
    status, body = ask(session)
    assert status == 200
    assert not body["abstained"]
    assert "per-session basis" in body["answer"]
    assert {row["tenant_id"] for row in body["citations"]} == {"alpha"}
    assert all("reader" in row["allowed_roles"] for row in body["citations"])
    assert {row["provenance"]["method"] for row in body["citations"]} == {"native"}
    verify_exact_provenance(body, by_id)


def test_public_image_only_pdf_uses_real_ocr_and_original_page_mapping(pdf_http):
    session, by_id = pdf_http
    status, body = ask(session, "analyst-alpha", question=OCR_QUESTION)
    assert status == 200
    assert not body["abstained"], body
    assert all(word in body["answer"] for word in ("GOVERN", "MAP", "MEASURE", "MANAGE"))
    assert {row["provenance"]["method"] for row in body["citations"]} == {"ocr"}
    assert {row["provenance"]["source_page"] for row in body["citations"]} == {25}
    assert {row["provenance"]["artifact_page"] for row in body["citations"]} == {1}
    assert all(row["provenance"]["ocr_words"] for row in body["citations"])
    assert all(row["provenance"]["source_sha256"] != row["provenance"]["artifact_sha256"]
               for row in body["citations"])
    verify_exact_provenance(body, by_id)


@pytest.mark.parametrize("client,question", [("reader-alpha", OCR_QUESTION), ("reader-beta", OCR_QUESTION)])
def test_pdf_acl_role_filter_prevents_unauthorized_evidence_over_real_http(pdf_http, client, question):
    session, _ = pdf_http
    tenant = "beta" if client == "reader-beta" else "alpha"
    status, body = ask(session, client, tenant=tenant, question=question)
    assert status == 200
    assert body["abstained"]
    assert body["citations"] == []
    assert body["evidence"] == []


def test_identical_public_pdf_bytes_are_not_deduplicated_across_tenants(pdf_http):
    session, by_id = pdf_http
    left_status, left = ask(session)
    right_status, right = ask(session, "reader-beta", tenant="beta")
    assert left_status == right_status == 200
    assert left["answer"] == right["answer"]
    assert {row["document_id"] for row in left["citations"]}.isdisjoint(
        {row["document_id"] for row in right["citations"]})
    assert {row["provenance"]["source_sha256"] for row in left["citations"]} == {
        row["provenance"]["source_sha256"] for row in right["citations"]}
    assert {row["tenant_id"] for row in right["citations"]} == {"beta"}
    verify_exact_provenance(right, by_id)


def test_cross_tenant_and_role_override_attempts_fail_before_pdf_retrieval(pdf_http):
    session, _ = pdf_http
    status, body = ask(session, tenant="beta")
    assert status == 403 and set(body) == {"error"}
    status, body = ask(session, body={"query": OCR_QUESTION, "roles": ["analyst"], "top_k": 3})
    assert status == 422 and set(body) == {"error"}
    status, body = ask(session, headers={"x-role": "analyst"})
    assert status == 400 and set(body) == {"error"}


def test_pdf_citations_and_permissions_survive_resource_process_restart(pdf_http):
    session, by_id = pdf_http
    before_status, before = ask(session, "analyst-alpha", question=OCR_QUESTION)
    old_pid = session.server._process._child.pid
    session.restart()
    assert session.server._process._child.pid != old_pid
    after_status, after = ask(session, "analyst-alpha", question=OCR_QUESTION)
    assert before_status == after_status == 200
    assert before["answer"] == after["answer"]
    assert citation_content(before["citations"]) == citation_content(after["citations"])
    verify_exact_provenance(after, by_id)
    assert ask(session, question=OCR_QUESTION)[1]["citations"] == []


def test_pdf_reingestion_replaces_alpha_acl_without_overwriting_beta(real_embedder, tmp_path):
    from enterprise_ai_lab.rag.store import VectorStore
    path = write_grants(tmp_path / "inputs")
    store_path = tmp_path / "vectors"
    store = VectorStore(store_path, real_embedder)
    report = ingest_pdf_manifest(path, store)
    store.close()
    ids = {page["tenant_id"]: page["document_id"] for page in report["pages"] if page["provenance"]["method"] == "native"}
    with RagSession(os.environ["IDENTITY_LAB_DIR"], Path(os.environ["RAG_MODEL_DIR"]), store_path,
                    node=os.environ.get("NODE_BINARY")) as session:
        assert "per-session basis" in ask(session)[1]["answer"]
        session.server.close()
        grant = json.loads(path.read_text())
        grant["entries"][0]["allowed_roles"] = ["analyst"]
        path.write_text(json.dumps(grant))
        store = VectorStore(store_path, real_embedder)
        try:
            updated = ingest_pdf_manifest(path, store)
            assert {page["tenant_id"]: page["document_id"] for page in updated["pages"]
                    if page["provenance"]["method"] == "native"} == ids
        finally:
            store.close()
        session.restart()
        status, reader = ask(session)
        assert status == 200 and reader["abstained"] and reader["citations"] == []
        status, analyst = ask(session, "analyst-alpha")
        assert status == 200 and "per-session basis" in analyst["answer"]
        assert all(row["allowed_roles"] == ["analyst"] for row in analyst["citations"])
        status, beta = ask(session, "reader-beta", tenant="beta")
        assert status == 200 and "per-session basis" in beta["answer"]
        assert all(row["tenant_id"] == "beta" and row["allowed_roles"] == ["reader"] for row in beta["citations"])
