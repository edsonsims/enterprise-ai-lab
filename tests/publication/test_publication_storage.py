"""Actual SQLite + Qdrant storage boundaries, with controlled constant vectors.

The end-to-end demo separately exercises the pinned real ONNX model. These tests
isolate publication authority, journaling and exact provenance from retrieval quality.
"""
from contextlib import contextmanager
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest
from temporalio.exceptions import ApplicationError

from enterprise_ai_lab.approval.contracts import DecisionCommand, DecisionReceipt
from enterprise_ai_lab.publication.activities import DocumentPublisher, PublicationBlocked
from enterprise_ai_lab.publication.contracts import ApplyApprovedDocuments, make_request
from enterprise_ai_lab.publication.storage import CandidateConflict, CandidateNotFound, CandidateRepository
from enterprise_ai_lab.rag.contracts import Document, EmbeddingLimitError, RagPrincipal
from enterprise_ai_lab.rag.provenance import SourceSpan, WordBox
from enterprise_ai_lab.rag.store import VectorStore


class ControlledEmbedder:
    dimensions = 3
    fingerprint = "f" * 64

    def embed(self, values):
        return [[1.0, 0.0, 0.0] for _ in values]


def page(document_id="alpha-page", tenant="alpha", roles=("reader",), *, ocr=False, text=None):
    text = text or "The Core includes GOVERN, MAP, MEASURE and MANAGE."
    text_hash = hashlib.sha256(text.encode()).hexdigest()
    provenance = SourceSpan("public-source", "https://example.org/public.pdf", "1" * 64, 25,
        "2" * 64 if ocr else "1" * 64, 1 if ocr else 25,
        "ocr" if ocr else "native", text_hash, 0, len(text),
        (WordBox("The", 0, 3, 10, 10, 20, 10, 97.5),) if ocr else (),
        180 if ocr else None, 200 if ocr else None, 300 if ocr else None)
    return Document(document_id, tenant, roles, "Public source", text, provenance)


def approved(repository, documents, *, request_id="request-one", revision=0, decision="approve",
             actor="reviewer", roles=("approver",), accepted_at="2026-10-18T12:00:00Z"):
    candidate = repository.stage(documents)
    request = make_request(request_id, candidate.tenant_id, "requester", candidate.candidate_sha256,
                           revision, "2026-10-18T13:00:00Z")
    command = DecisionCommand("command-one", request.tenant_id, request.snapshot_sha256,
                              decision, actor, roles)
    return ApplyApprovedDocuments(request, DecisionReceipt(command, accepted_at))


@pytest.fixture
def setup(tmp_path):
    repository = CandidateRepository(tmp_path / "candidates.sqlite")
    store = VectorStore(tmp_path / "vectors", ControlledEmbedder())
    publisher = DocumentPublisher(repository, store)
    yield repository, store, publisher
    store.close()


def test_staging_is_content_addressed_and_never_publishes(setup):
    repository, store, publisher = setup
    documents = [page(), page("alpha-ocr", roles=("analyst",), ocr=True)]
    first, second = repository.stage(documents), repository.stage(reversed(documents))
    assert first == second and first.document_count == 2
    assert repository.load_documents("alpha", first.candidate_sha256) == sorted(documents, key=lambda row: row.document_id)
    assert all("text" not in row for row in first.documents)
    assert first.documents[0]["provenance"]["source_page"] == 25
    assert store.client.count(store.collection_name, exact=True).count == 0
    assert repository.revision("alpha") == 0 and repository.list_effects() == []
    publisher.assert_readable()


def test_candidate_hash_binds_acl_text_and_complete_provenance(setup):
    repository, _, _ = setup
    original = page(ocr=True)
    baseline = repository.stage([original]).candidate_sha256
    candidates = [replace(original, allowed_roles=("analyst",)),
                  replace(original, title="Different title"),
                  replace(original, provenance=replace(original.provenance, source_uri="https://example.org/other.pdf")),
                  replace(original, provenance=replace(original.provenance,
                      ocr_words=(replace(original.provenance.ocr_words[0], confidence=96.0),))),
                  page(ocr=True, text="The other Core sentence describes a different source.")]
    assert all(repository.stage([row]).candidate_sha256 != baseline for row in candidates)


def test_candidate_retrieval_is_tenant_scoped_without_metadata_leak(setup):
    repository, _, _ = setup
    candidate = repository.stage([page()])
    with pytest.raises(CandidateNotFound):
        repository.get_candidate("beta", candidate.candidate_sha256)
    with pytest.raises(CandidateNotFound):
        repository.load_documents("alpha", "0" * 64)


@pytest.mark.parametrize("change", ["text", "roles", "provenance"])
def test_every_candidate_load_rehashes_stored_content(setup, change):
    repository, _, _ = setup
    candidate = repository.stage([page()])
    connection = sqlite3.connect(repository.database_path)
    encoded = connection.execute("SELECT candidate_json FROM publication_candidates").fetchone()[0]
    value = json.loads(encoded)
    document = value["documents"][0]
    if change == "text":
        document["text"] = "Modified after staging"
    elif change == "roles":
        document["allowed_roles"] = ["reader", "analyst"]
    else:
        document["provenance"]["source_page"] = 24
    connection.execute("UPDATE publication_candidates SET candidate_json=?", (json.dumps(value),))
    connection.commit()
    connection.close()
    with pytest.raises(CandidateConflict):
        repository.load_documents("alpha", candidate.candidate_sha256)
    with pytest.raises(CandidateConflict):
        repository.get_candidate("alpha", candidate.candidate_sha256)


@pytest.mark.parametrize("documents", [[], [page(), page()], [page(), page("beta", "beta")]])
def test_empty_duplicate_and_cross_tenant_batches_fail_without_effects(setup, documents):
    repository, _, _ = setup
    with pytest.raises(CandidateConflict):
        repository.stage(documents)
    assert repository.list_effects() == []


@pytest.mark.parametrize("options", [
    {"decision": "reject"}, {"actor": "requester"}, {"roles": ("reader",)},
    {"accepted_at": "2026-10-18T13:00:00Z"},
])
def test_rejected_self_unauthorized_and_expired_receipts_never_publish(setup, options):
    repository, store, publisher = setup
    value = approved(repository, [page()], **options)
    with pytest.raises(ApplicationError) as error:
        publisher.publish_approved_documents(value)
    assert error.value.non_retryable and error.value.type == "CandidateConflict"
    assert store.client.count(store.collection_name, exact=True).count == 0
    assert repository.revision("alpha") == 0 and repository.list_effects() == []
    publisher.assert_readable()


def test_actual_qdrant_publication_preserves_tenant_acl_unicode_and_ocr(setup):
    repository, store, publisher = setup
    documents = [page(text="Açúcar is a Unicode source sentence."),
                 page("alpha-ocr", roles=("analyst",), ocr=True)]
    value = approved(repository, documents)
    effect = publisher.publish_approved_documents(value)
    assert effect.revision == 1 and effect.document_count == 2 and effect.chunk_count == 2
    assert repository.revision("alpha") == 1
    assert repository.list_effects() == [asdict(effect)]
    publisher.assert_readable()
    assert not store.search("Core", RagPrincipal("beta", ("reader", "analyst"), "beta-reader"))
    readers = store.search("Core", RagPrincipal("alpha", ("reader",), "alpha-reader"))
    analysts = store.search("Core", RagPrincipal("alpha", ("analyst",), "alpha-analyst"))
    assert [row.document_id for row in readers] == ["alpha-page"]
    assert readers[0].provenance == documents[0].provenance
    assert [row.document_id for row in analysts] == ["alpha-ocr"]
    assert analysts[0].provenance == documents[1].provenance


def test_old_successful_delivery_cannot_republish_after_newer_acl_revision(setup):
    repository, store, publisher = setup
    first = approved(repository, [page()])
    first_effect = publisher.publish_approved_documents(first)
    second = approved(repository, [page(roles=("analyst",))], request_id="request-two", revision=1)
    second_effect = publisher.publish_approved_documents(second)
    assert publisher.publish_approved_documents(first) == first_effect
    assert first_effect.revision == 1 and second_effect.revision == 2
    assert repository.revision("alpha") == 2 and len(repository.list_effects()) == 2
    assert not store.search("Core", RagPrincipal("alpha", ("reader",), "reader"))
    assert store.search("Core", RagPrincipal("alpha", ("analyst",), "analyst"))


def test_conflicting_immutable_receipt_and_stale_cas_never_overwrite(setup):
    repository, store, publisher = setup
    first = approved(repository, [page()])
    publisher.publish_approved_documents(first)
    changed_receipt = replace(first, receipt=replace(first.receipt,
        command=replace(first.receipt.command, command_id="different-command")))
    with pytest.raises(ApplicationError, match="conflicts"):
        publisher.publish_approved_documents(changed_receipt)
    stale = approved(repository, [page(roles=("analyst",))], request_id="stale", revision=0)
    with pytest.raises(ApplicationError, match="conflicts"):
        publisher.publish_approved_documents(stale)
    assert repository.revision("alpha") == 1
    assert store.search("Core", RagPrincipal("alpha", ("reader",), "reader"))


def test_global_document_id_cannot_overwrite_another_tenant(setup):
    repository, store, publisher = setup
    publisher.publish_approved_documents(approved(repository, [page("same-id")]))
    beta = approved(repository, [page("same-id", "beta")])
    with pytest.raises(ApplicationError, match="conflicts"):
        publisher.publish_approved_documents(beta)
    assert repository.revision("beta") == 0
    assert not store.search("Core", RagPrincipal("beta", ("reader",), "beta-reader"))
    assert store.search("Core", RagPrincipal("alpha", ("reader",), "alpha-reader"))


def test_same_request_id_is_independent_across_tenants(setup):
    repository, _, publisher = setup
    alpha = publisher.publish_approved_documents(approved(repository, [page()]))
    beta = publisher.publish_approved_documents(approved(repository, [page("beta-page", "beta")]))
    assert alpha.revision == beta.revision == 1
    assert len(repository.list_effects()) == 2
    assert len(repository.list_effects("beta")) == 1
    publisher.assert_readable()


def test_omitted_documents_are_retained_in_later_selected_batch(setup):
    repository, store, publisher = setup
    publisher.publish_approved_documents(approved(repository, [page(), page("alpha-second")]))
    publisher.publish_approved_documents(approved(repository, [page(roles=("analyst",))], request_id="second", revision=1))
    readers = store.search("Core", RagPrincipal("alpha", ("reader",), "reader"))
    assert [row.document_id for row in readers] == ["alpha-second"]
    publisher.assert_readable()


def test_commit_before_ack_failure_is_idempotent_without_second_index(setup, monkeypatch):
    repository, store, _ = setup
    publisher = DocumentPublisher(repository, store, fault_once_after_commit=True)
    value = approved(repository, [page()])
    with pytest.raises(RuntimeError, match="lost acknowledgement"):
        publisher.publish_approved_documents(value)
    assert repository.revision("alpha") == 1 and len(repository.list_effects()) == 1
    def no_second_index(_):
        raise AssertionError("committed replay must not write the vector store")
    monkeypatch.setattr(store, "_prepared_index", no_second_index)
    assert publisher.publish_approved_documents(value).revision == 1
    publisher.assert_readable()


def test_crash_gap_after_vector_success_is_fenced_and_exact_retry_recovers(setup, monkeypatch):
    repository, store, publisher = setup
    value = approved(repository, [page()])
    prepare = store._prepared_index
    @contextmanager
    def after_vector_success(documents):
        with prepare(documents) as apply:
            def crash():
                apply()
                raise RuntimeError("process lost before ledger commit")
            yield crash
    monkeypatch.setattr(store, "_prepared_index", after_vector_success)
    with pytest.raises(RuntimeError, match="ledger commit"):
        publisher.publish_approved_documents(value)
    assert repository.list_effects() == [] and repository.revision("alpha") == 0
    assert not (store.path / "rag-write-pending").exists()
    with pytest.raises(PublicationBlocked, match="unfinished"):
        publisher.assert_readable()
    monkeypatch.setattr(store, "_prepared_index", prepare)
    assert publisher.publish_approved_documents(value).revision == 1
    publisher.assert_readable()


def test_restart_in_vector_ledger_gap_stays_fenced_and_recovers(tmp_path, monkeypatch):
    repository = CandidateRepository(tmp_path / "candidates.sqlite")
    path = tmp_path / "vectors"
    first = VectorStore(path, ControlledEmbedder())
    publisher = DocumentPublisher(repository, first)
    value = approved(repository, [page()])
    prepare = first._prepared_index
    @contextmanager
    def crash(documents):
        with prepare(documents) as apply:
            def failed_ack():
                apply()
                raise RuntimeError("crash gap")
            yield failed_ack
    monkeypatch.setattr(first, "_prepared_index", crash)
    with pytest.raises(RuntimeError):
        publisher.publish_approved_documents(value)
    first.close()
    second = VectorStore(path, ControlledEmbedder())
    try:
        restarted = DocumentPublisher(repository, second)
        with pytest.raises(PublicationBlocked):
            restarted.assert_readable()
        restarted.publish_approved_documents(value)
        restarted.assert_readable()
        assert repository.revision("alpha") == 1
    finally:
        second.close()


def test_partial_qdrant_write_keeps_both_fences_and_cannot_be_retried(setup, monkeypatch):
    repository, store, publisher = setup
    value = approved(repository, [page()])
    def failed_upsert(*args, **kwargs):
        raise RuntimeError("partial Qdrant failure")
    monkeypatch.setattr(store.client, "upsert", failed_upsert)
    with pytest.raises(RuntimeError, match="partial Qdrant"):
        publisher.publish_approved_documents(value)
    assert (store.path / "rag-write-pending").exists()
    assert repository.list_effects() == [] and repository.revision("alpha") == 0
    with pytest.raises(RuntimeError, match="incomplete local index mutation"):
        publisher.assert_readable()
    with pytest.raises(RuntimeError, match="incomplete local index mutation"):
        publisher.publish_approved_documents(value)


def test_embedding_preparation_failure_keeps_healthy_index_readable(setup, monkeypatch):
    repository, store, publisher = setup
    publisher.publish_approved_documents(approved(repository, [page()]))
    value = approved(repository, [page(text="The oversized input fails preparation.")], request_id="oversized", revision=1)
    embed = store.embedder.embed
    def fail_before_mutation(values):
        raise EmbeddingLimitError("controlled tokenizer limit")
    monkeypatch.setattr(store.embedder, "embed", fail_before_mutation)
    with pytest.raises(EmbeddingLimitError):
        publisher.publish_approved_documents(value)
    monkeypatch.setattr(store.embedder, "embed", embed)
    connection = sqlite3.connect(repository.database_path)
    assert connection.execute("SELECT count(*) FROM publication_effects WHERE state='pending'").fetchone()[0] == 0
    connection.close()
    assert not (store.path / "rag-write-pending").exists()
    publisher.assert_readable()
    assert repository.revision("alpha") == 1
    assert store.search("Core", RagPrincipal("alpha", ("reader",), "reader"))[0].text == page().text
    next_value = approved(repository, [page(roles=("analyst",))], request_id="valid-next", revision=1)
    assert publisher.publish_approved_documents(next_value).revision == 2


def test_prepared_index_embeds_once_and_only_applies_inside_context(setup, monkeypatch):
    _, store, _ = setup
    calls = []
    embed = store.embedder.embed
    def capture(values):
        calls.append(list(values))
        return embed(values)
    monkeypatch.setattr(store.embedder, "embed", capture)
    with store._prepared_index([page()]) as apply:
        assert len(calls) == 1
        assert store.client.count(store.collection_name, exact=True).count == 0
        assert not (store.path / "rag-write-pending").exists()
        assert apply() == 1
        with pytest.raises(RuntimeError, match="no longer available"):
            apply()
    with pytest.raises(RuntimeError, match="no longer available"):
        apply()
    assert len(calls) == 1
    assert not (store.path / "rag-write-pending").exists()


def test_publisher_rejects_existing_direct_ingestion(tmp_path):
    store = VectorStore(tmp_path / "vectors", ControlledEmbedder())
    try:
        store.index([page()])
        with pytest.raises(PublicationBlocked, match="empty unowned"):
            DocumentPublisher(CandidateRepository(tmp_path / "candidates.sqlite"), store)
    finally:
        store.close()


def test_direct_unledgered_mutation_blocks_all_queries(setup):
    _, store, publisher = setup
    store.index([page()])
    with pytest.raises(PublicationBlocked, match="inventory differs"):
        publisher.assert_readable()


def test_syntactically_valid_ledger_and_vector_change_still_needs_immutable_candidate(setup):
    repository, store, publisher = setup
    publisher.publish_approved_documents(approved(repository, [page()]))
    changed = page(roles=("analyst",))
    # Simulate damaged local state in both stores. Matching vector/ledger
    # payloads cannot invent a candidate that passed the immutable approval.
    store.index([changed])
    connection = sqlite3.connect(repository.database_path)
    encoded = connection.execute("SELECT document_json FROM publication_documents").fetchone()[0]
    value = json.loads(encoded)
    value["allowed_roles"] = ["analyst"]
    connection.execute("UPDATE publication_documents SET document_json=?", (json.dumps(value),))
    connection.commit()
    connection.close()
    with pytest.raises(PublicationBlocked, match="committed publication inventory"):
        publisher.assert_readable()


def test_committed_candidate_corruption_blocks_reads(setup):
    repository, _, publisher = setup
    publisher.publish_approved_documents(approved(repository, [page()]))
    connection = sqlite3.connect(repository.database_path)
    connection.execute("UPDATE publication_candidates SET candidate_json='{}'")
    connection.commit()
    connection.close()
    with pytest.raises(PublicationBlocked, match="committed publication inventory"):
        publisher.assert_readable()


def test_ledger_cannot_silently_bind_a_different_fresh_index(setup, tmp_path):
    repository, _, _ = setup
    other = VectorStore(tmp_path / "other-index", ControlledEmbedder())
    try:
        with pytest.raises(PublicationBlocked, match="marker"):
            DocumentPublisher(repository, other)
    finally:
        other.close()


def test_missing_ledger_cannot_adopt_an_owned_index(setup, tmp_path):
    _, store, _ = setup
    with pytest.raises(PublicationBlocked, match="empty unowned"):
        DocumentPublisher(CandidateRepository(tmp_path / "different.sqlite"), store)


def test_publication_store_and_ledger_survive_clean_restart(tmp_path):
    repository = CandidateRepository(tmp_path / "candidates.sqlite")
    first = VectorStore(tmp_path / "vectors", ControlledEmbedder())
    value = approved(repository, [page(ocr=True)])
    effect = DocumentPublisher(repository, first).publish_approved_documents(value)
    first.close()
    second = VectorStore(tmp_path / "vectors", ControlledEmbedder())
    try:
        restarted = DocumentPublisher(repository, second)
        restarted.assert_readable()
        assert restarted.publish_approved_documents(value) == effect
        rows = second.search("Core", RagPrincipal("alpha", ("reader",), "reader"))
        assert rows[0].provenance == page(ocr=True).provenance
    finally:
        second.close()
