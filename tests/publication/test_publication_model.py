"""Pinned actual MiniLM input failure must not fence previously approved data.

These tests use actual ONNX embeddings, SQLite and Qdrant. Approval receipts are
explicit controlled fixtures; the separate process suite exercises Temporal and
the actual OAuth issuer. No model is downloaded by this test.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import sqlite3

import pytest

from enterprise_ai_lab.approval.contracts import DecisionCommand, DecisionReceipt, format_utc
from enterprise_ai_lab.publication.activities import DocumentPublisher
from enterprise_ai_lab.publication.contracts import ApplyApprovedDocuments, make_request
from enterprise_ai_lab.publication.storage import CandidateRepository
from enterprise_ai_lab.rag.contracts import Document, EmbeddingLimitError, RagPrincipal
from enterprise_ai_lab.rag.models import LocalEmbedder
from enterprise_ai_lab.rag.store import VectorStore


@pytest.mark.skipif(os.environ.get("RUN_RAG_INTEGRATION") != "1",
                    reason="Enable RUN_RAG_INTEGRATION=1 with the pinned RAG_MODEL_DIR.")
def test_actual_model_rejection_preserves_old_publication_and_allows_later_revision(tmp_path):
    model = LocalEmbedder(Path(os.environ["RAG_MODEL_DIR"]))
    repository = CandidateRepository(tmp_path / "publication.sqlite")
    store = VectorStore(tmp_path / "vectors", model)
    publisher = DocumentPublisher(repository, store)
    principal = RagPrincipal("alpha", ("reader",), "reader-alpha")
    first_text = "Access to individual enterprise resources is granted on a per-session basis."

    def approved(request_id, text, revision):
        candidate = repository.stage([Document("alpha-page", "alpha", ("reader",),
                                               "Synthetic publication fixture", text)])
        now = datetime.now(timezone.utc)
        request = make_request(request_id, "alpha", "requester-alpha", candidate.candidate_sha256,
                               revision, format_utc(now + timedelta(minutes=5)))
        command = DecisionCommand(request_id, "alpha", request.snapshot_sha256,
                                   "approve", "approver-alpha", ("approver",))
        return ApplyApprovedDocuments(request, DecisionReceipt(command, format_utc(now)))

    try:
        first = publisher.publish_approved_documents(approved("first", first_text, 0))
        assert first.revision == 1
        publisher.assert_readable()
        # This valid <=500-character chunk exceeds the actual pinned tokenizer
        # context. Character limits alone cannot decide embedding admissibility.
        over_limit = "界" * 400
        with pytest.raises(EmbeddingLimitError, match="256 tokens"):
            publisher.publish_approved_documents(approved("too-many-tokens", over_limit, 1))
        connection = sqlite3.connect(repository.database_path)
        try:
            pending = connection.execute("SELECT count(*) FROM publication_effects WHERE state!='committed'").fetchone()[0]
        finally:
            connection.close()
        assert pending == 0, "A pre-mutation model rejection must not create an outer publication fence."
        assert not (store.path / "rag-write-pending").exists()
        assert repository.revision("alpha") == 1
        assert len(repository.list_effects()) == 1
        publisher.assert_readable()
        rows = store.search("How is access to individual enterprise resources granted?", principal)
        assert len(rows) == 1 and rows[0].text == first_text

        replacement = "Access to individual enterprise resources requires a reviewed session."
        second = publisher.publish_approved_documents(approved("valid-replacement", replacement, 1))
        assert second.revision == 2 and repository.revision("alpha") == 2
        assert len(repository.list_effects()) == 2
        publisher.assert_readable()
        rows = store.search("How is access to individual enterprise resources granted?", principal)
        assert len(rows) == 1 and rows[0].text == replacement
    finally:
        store.close()
