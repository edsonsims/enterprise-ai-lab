"""Actual CPU ONNX embeddings + persistent Qdrant local-mode integration tests.

Set RAG_MODEL_DIR after the explicit pinned model bootstrap. No downloads occur
here, and no fake embedding engine substitutes for the model in these tests.
"""
import json
import math
import os
from pathlib import Path
import socket

import pytest
from qdrant_client import models

from enterprise_ai_lab.rag.contracts import Document, QueryLimitError, RagPrincipal
from enterprise_ai_lab.rag.fixtures import fixture_path, load_documents
from enterprise_ai_lab.rag.models import LocalEmbedder, model_lock, verify_model
from enterprise_ai_lab.rag.pipeline import RagPipeline
from enterprise_ai_lab.rag.store import UnsafePayloadError, VectorStore, chunk_document


@pytest.fixture(scope="module")
def model_dir():
    value = os.environ.get("RAG_MODEL_DIR")
    if not value:
        pytest.skip("Set RAG_MODEL_DIR after explicit pinned model bootstrap to run actual vector integration")
    return Path(value)


@pytest.fixture(scope="module")
def embedder(model_dir):
    return LocalEmbedder(model_dir)


@pytest.fixture
def store(tmp_path, embedder):
    value = VectorStore(tmp_path / "vectors", embedder)
    value.index(load_documents())
    yield value
    value.close()


def principal(tenant="alpha", roles=("reader",)):
    return RagPrincipal(tenant, roles, "test-client")


def test_model_is_real_pinned_finite_384_dimensional_cpu_inference(embedder):
    vectors = embedder.embed(["document processing limit", "reserve buffer"])
    assert len(vectors) == 2
    assert all(len(vector) == 384 and all(math.isfinite(value) for value in vector) for vector in vectors)
    assert vectors[0] != vectors[1]
    assert embedder.manifest["revision"] == "d13954661f83248295ba75c1ed411eef3b7b936e"


def test_offline_model_load_and_search_never_open_a_network_connection(model_dir, tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError("network access attempted during offline inference")
    monkeypatch.setattr(socket.socket, "connect", deny_network)
    model = LocalEmbedder(model_dir)
    vector_store = VectorStore(tmp_path / "offline", model)
    try:
        vector_store.index(load_documents())
        result = RagPipeline(vector_store).query("What is the document processing limit?", principal())
        assert "20" in result["answer"]
    finally:
        vector_store.close()


@pytest.mark.parametrize("tenant,number", [("alpha", "20"), ("beta", "75")])
def test_same_question_yields_only_authenticated_tenant_passages(store, tenant, number):
    result = RagPipeline(store).query("What is the document processing limit?", principal(tenant))
    assert not result["abstained"]
    assert number in result["answer"]
    assert {row["document_id"] for row in result["citations"]} == {tenant + "-onboarding"}
    assert all(row["tenant_id"] == tenant for row in result["citations"])
    assert all(row["quote"] in row["text"] for row in result["citations"])


@pytest.mark.parametrize("tenant,number", [("alpha", "18"), ("beta", "42")])
def test_role_protected_policy_is_not_available_to_reader(store, tenant, number):
    pipeline = RagPipeline(store)
    denied = pipeline.query("What is the reserve buffer?", principal(tenant))
    assert denied["abstained"] and denied["citations"] == []
    assert "INTERNAL_RESERVE" not in json.dumps(denied)
    assert denied["generation_performed"] is False
    allowed = pipeline.query("What is the reserve buffer?", principal(tenant, ("reader", "analyst")))
    assert not allowed["abstained"]
    assert number in allowed["answer"]
    assert {row["document_id"] for row in allowed["citations"]} == {tenant + "-reserve"}


def test_unanswerable_question_abstains_without_context_or_generation(store):
    result = RagPipeline(store).query("What is the Neptune launch password?", principal())
    assert result["abstained"] and not result["citations"] and not result["evidence"]
    assert result["generator"] == "extractive-v1" and result["generation_performed"] is False


def test_policy_filter_is_passed_to_real_engine_before_top_k(store, monkeypatch):
    captured = {}
    query = store.client.query_points
    def capture(*args, **kwargs):
        captured.update(kwargs)
        return query(*args, **kwargs)
    monkeypatch.setattr(store.client, "query_points", capture)
    rows = store.search("What is the document processing limit?", principal(), top_k=1)
    assert len(rows) == 1 and rows[0].document_id == "alpha-onboarding"
    assert captured["limit"] == 1
    assert captured["query_filter"].must == [
        models.FieldCondition(key="tenant_id", match=models.MatchValue(value="alpha")),
        models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=["reader"])),
    ]
    assert len(captured["query"]) == 384


def test_real_vector_index_survives_close_and_reopen(tmp_path, embedder):
    path = tmp_path / "persistent"
    first = VectorStore(path, embedder)
    assert first.index(load_documents()) == 8
    before = first.search("What is the reserve buffer?", principal(roles=("analyst",)))
    first.close()
    second = VectorStore(path, embedder)
    try:
        after = second.search("What is the reserve buffer?", principal(roles=("analyst",)))
        assert [{key: value for key, value in row.as_dict().items() if key != "score"} for row in before] == [
            {key: value for key, value in row.as_dict().items() if key != "score"} for row in after]
        # Qdrant persists float32 vectors and re-normalizes them on load; exact
        # decimal score equality is not part of the storage contract.
        assert [row.score for row in after] == pytest.approx([row.score for row in before], abs=1e-6)
    finally:
        second.close()


def test_document_acl_revocation_is_immediate_and_persistent(tmp_path, embedder):
    path = tmp_path / "acl"
    value = VectorStore(path, embedder)
    value.index(load_documents())
    assert value.search("What is the document processing limit?", principal())
    value.update_acl("alpha-onboarding", "alpha", ("analyst",))
    assert not value.search("What is the document processing limit?", principal())
    assert value.search("What is the document processing limit?", principal(roles=("analyst",)))
    value.close()
    reopened = VectorStore(path, embedder)
    try:
        assert not reopened.search("What is the document processing limit?", principal())
    finally:
        reopened.close()


def test_acl_update_cannot_target_another_tenant(store):
    store.update_acl("alpha-onboarding", "beta", ("analyst",))
    assert store.search("What is the document processing limit?", principal())
    with pytest.raises(ValueError):
        store.update_acl("alpha-onboarding", "alpha", ())


def test_delete_document_removes_authorized_context(store):
    store.delete_document("alpha-reserve")
    assert not store.search("What is the reserve buffer?", principal(roles=("analyst",)))
    assert store.search("What is the reserve buffer?", principal("beta", ("analyst",)))


def test_reindex_replaces_old_chunks_and_acl_instead_of_leaving_stale_context(store):
    replacement = Document("alpha-onboarding", "alpha", ("analyst",), "New synthetic policy",
        "Alpha's document processing limit is 33 documents per processing batch.")
    assert store.index([replacement]) == 1
    assert not store.search("What is the document processing limit?", principal())
    answer = RagPipeline(store).query("What is the document processing limit?", principal(roles=("analyst",)))
    assert "33" in answer["answer"] and "20" not in json.dumps(answer)
    rows, cursor = store.client.scroll(store.collection_name, scroll_filter=models.Filter(must=[
        models.FieldCondition(key="document_id", match=models.MatchValue(value="alpha-onboarding"))]), limit=10)
    assert len(rows) == 1 and cursor is None


def test_result_payload_is_revalidated_after_vector_engine_filter(store):
    store.client.set_payload(store.collection_name, payload={"title": 123}, points=models.Filter(must=[
        models.FieldCondition(key="document_id", match=models.MatchValue(value="alpha-onboarding"))]))
    with pytest.raises(UnsafePayloadError):
        RagPipeline(store).query("What is the document processing limit?", principal())


@pytest.mark.parametrize("question,top_k", [("", 1), (" " * 3, 1), ("x" * 2001, 1), ("ok", 0), ("ok", 6), ("ok", True)])
def test_query_input_bounds_fail_before_vector_search(store, question, top_k):
    with pytest.raises(QueryLimitError):
        store.search(question, principal(), top_k)


def test_token_limit_does_not_silently_truncate_question(store):
    with pytest.raises(QueryLimitError, match="256 tokens"):
        store.search("word " * 300, principal())


def test_plain_request_dictionary_cannot_be_used_as_authenticated_principal(store):
    with pytest.raises(ValueError):
        store.search("What is the reserve buffer?", {"tenant_id": "alpha", "roles": ["analyst"]})


def test_incompatible_embedding_manifest_refuses_index_reuse(tmp_path, embedder):
    path = tmp_path / "manifest"
    value = VectorStore(path, embedder)
    value.close()
    manifest = path / "rag-index.json"
    original = json.loads(manifest.read_text())
    manifest.write_text(json.dumps({**original, "model_sha256": "0" * 64}))
    with pytest.raises(ValueError, match="fingerprint differs"):
        VectorStore(path, embedder)


def test_missing_or_changed_pinned_asset_is_rejected_without_network(tmp_path, monkeypatch):
    import enterprise_ai_lab.rag.models as module
    (tmp_path / "config.json").write_text("{}")
    monkeypatch.setattr(module, "model_lock", lambda: {"files": {"config.json": {"size": 2, "sha256": "0" * 64}}})
    with pytest.raises(ValueError, match="hash mismatch"):
        verify_model(tmp_path)
    (tmp_path / "config.json").unlink()
    with pytest.raises(ValueError, match="missing or changed"):
        verify_model(tmp_path)


def test_fixture_provenance_and_chunk_boundaries_are_explicit():
    documents = load_documents()
    assert len(documents) == 6
    assert all("synthetic" in document.title for document in documents)
    chunks = [chunk for document in documents for chunk in chunk_document(document)]
    assert len(chunks) == 8
    assert all(len(chunk["text"]) <= 500 for chunk in chunks)
    assert len({chunk["chunk_id"] for chunk in chunks}) == len(chunks)
    assert fixture_path().is_file()


@pytest.mark.parametrize("kwargs", [
    {"document_id": "bad/id"}, {"tenant_id": ""}, {"allowed_roles": ()},
    {"allowed_roles": ("reader", "reader")}, {"text": " "}, {"text": "\ud800"},
])
def test_invalid_document_and_acl_metadata_are_rejected(kwargs):
    values = dict(document_id="doc", tenant_id="alpha", allowed_roles=("reader",), title="Title", text="text")
    with pytest.raises(ValueError):
        Document(**{**values, **kwargs})
