"""Immutable, content-addressed candidates in a trusted local SQLite repository.

Staging never writes the vector index.  Every read checks the complete canonical
document bytes and reconstructs the strict Document/SourceSpan contracts.  The
database contains full text and must be treated as private operator state.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

from ..approval.contracts import identifier
from ..rag.contracts import Document
from ..rag.provenance import SourceSpan
from ..rag.store import chunk_document
from .contracts import CandidateMetadata, digest


MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
MAX_CANDIDATE_DOCUMENTS = 100
MAX_CANDIDATE_CHUNKS = 2000


class CandidateNotFound(LookupError):
    """No candidate is visible in the authenticated tenant."""


class CandidateConflict(ValueError):
    """Stored bytes or a content address failed validation."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def document_dict(document: Document) -> dict:
    # Reconstructing also rejects object.__setattr__ changes to a frozen record.
    Document(document.document_id, document.tenant_id, document.allowed_roles,
             document.title, document.text, document.provenance)
    value = {"document_id": document.document_id, "tenant_id": document.tenant_id,
             "allowed_roles": list(document.allowed_roles), "title": document.title,
             "text": document.text, "provenance": None}
    if document.provenance is not None:
        value["provenance"] = document.provenance.as_dict()
    return value


def parse_document(value: object) -> Document:
    if (not isinstance(value, dict)
            or set(value) != {"document_id", "tenant_id", "allowed_roles", "title", "text", "provenance"}
            or not isinstance(value["allowed_roles"], list)
            or not 1 <= len(value["allowed_roles"]) <= 16):
        raise CandidateConflict("invalid staged document schema")
    provenance = SourceSpan.from_dict(value["provenance"]) if value["provenance"] is not None else None
    return Document(value["document_id"], value["tenant_id"], tuple(value["allowed_roles"]),
                    value["title"], value["text"], provenance)


def _documents(values: object, tenant_id: str) -> list[Document]:
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_CANDIDATE_DOCUMENTS:
        raise CandidateConflict("invalid candidate document count")
    documents = [parse_document(value) for value in values]
    if (any(row.tenant_id != tenant_id for row in documents)
            or len({row.document_id for row in documents}) != len(documents)):
        raise CandidateConflict("candidate identity differs or contains duplicates")
    if sum(len(chunk_document(row)) for row in documents) > MAX_CANDIDATE_CHUNKS:
        raise CandidateConflict("candidate exceeds the chunk limit")
    return documents


def _metadata(tenant_id: str, candidate_sha256: str, documents: list[Document]) -> CandidateMetadata:
    summaries = []
    for row in documents:
        summary = {"document_id": row.document_id, "title": row.title,
                   "allowed_roles": list(row.allowed_roles)}
        if row.provenance is not None:
            # The candidate hash still binds every OCR word box and exact span.
            # Review responses expose source identity, without full text/boxes.
            summary["provenance"] = {name: getattr(row.provenance, name) for name in (
                "source_id", "source_uri", "source_sha256", "source_page",
                "artifact_sha256", "artifact_page", "method", "page_text_sha256")}
        summaries.append(summary)
    return CandidateMetadata(tenant_id, candidate_sha256, len(documents),
                             tuple(row.document_id for row in documents), tuple(summaries))


class CandidateRepository:
    def __init__(self, sqlite_path: str | Path):
        self.database_path = Path(sqlite_path)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("""CREATE TABLE IF NOT EXISTS publication_candidates (
            candidate_sha256 TEXT PRIMARY KEY,
            tenant_id TEXT NOT NULL,
            candidate_json TEXT NOT NULL
        )""")
        return connection

    def stage(self, documents) -> CandidateMetadata:
        documents = list(documents)
        if not documents or any(not isinstance(row, Document) for row in documents):
            raise CandidateConflict("candidate needs validated documents")
        tenant_id = documents[0].tenant_id
        identifier(tenant_id)
        try:
            values = [document_dict(row) for row in documents]
            documents = _documents(values, tenant_id)
            # Sorting makes caller iteration order irrelevant; page text, ACL
            # order, Unicode and provenance remain byte-for-byte bound.
            values.sort(key=lambda row: row["document_id"])
            encoded = canonical({"schema": 1, "tenant_id": tenant_id, "documents": values})
            if len(encoded.encode("utf-8")) > MAX_CANDIDATE_BYTES:
                raise CandidateConflict("candidate exceeds the byte limit")
        except (ValueError, TypeError, UnicodeError) as exc:
            raise CandidateConflict("invalid candidate documents") from exc
        candidate_sha256 = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT tenant_id,candidate_json FROM publication_candidates WHERE candidate_sha256=?",
                                          (candidate_sha256,)).fetchone()
            if existing is None:
                connection.execute("INSERT INTO publication_candidates VALUES (?,?,?)", (candidate_sha256, tenant_id, encoded))
            elif existing != (tenant_id, encoded):
                raise CandidateConflict("candidate address conflicts with stored bytes")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_candidate(tenant_id, candidate_sha256)

    def load_documents(self, tenant_id: str, candidate_sha256: str) -> list[Document]:
        identifier(tenant_id)
        digest(candidate_sha256)
        connection = self._connect()
        try:
            # SQLite checks length before returning a potentially damaged blob.
            row = connection.execute("SELECT tenant_id,length(CAST(candidate_json AS BLOB)),"
                                     "CASE WHEN length(CAST(candidate_json AS BLOB))<=? THEN candidate_json ELSE NULL END "
                                     "FROM publication_candidates WHERE candidate_sha256=?",
                                     (MAX_CANDIDATE_BYTES, candidate_sha256)).fetchone()
        finally:
            connection.close()
        if row is None or row[0] != tenant_id:
            raise CandidateNotFound("candidate not found")
        try:
            if (type(row[1]) is not int or not 0 < row[1] <= MAX_CANDIDATE_BYTES
                    or not isinstance(row[2], str)):
                raise CandidateConflict("invalid candidate byte count")
            encoded = row[2]
            if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != candidate_sha256:
                raise CandidateConflict("candidate bytes differ from content address")
            value = json.loads(encoded)
            if (not isinstance(value, dict) or set(value) != {"schema", "tenant_id", "documents"}
                    or type(value["schema"]) is not int or value["schema"] != 1
                    or value["tenant_id"] != tenant_id or canonical(value) != encoded):
                raise CandidateConflict("invalid canonical candidate schema")
            return _documents(value["documents"], tenant_id)
        except (ValueError, TypeError, UnicodeError, KeyError) as exc:
            raise CandidateConflict("stored candidate failed validation") from exc

    def get_candidate(self, tenant_id: str, candidate_sha256: str) -> CandidateMetadata:
        return _metadata(tenant_id, candidate_sha256, self.load_documents(tenant_id, candidate_sha256))

    def revision(self, tenant_id: str) -> int:
        """Trusted local inspection; the initial tenant revision is zero."""
        identifier(tenant_id)
        connection = self._connect()
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_tenants'").fetchone() is None:
                return 0
            row = connection.execute("SELECT revision FROM publication_tenants WHERE tenant_id=?", (tenant_id,)).fetchone()
            return row[0] if row else 0
        finally:
            connection.close()

    def list_effects(self, tenant_id: str | None = None) -> list[dict]:
        """Committed receipt metadata for local demos, without candidate text."""
        if tenant_id is not None:
            identifier(tenant_id)
        connection = self._connect()
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='publication_effects'").fetchone() is None:
                return []
            sql = "SELECT effect_json FROM publication_effects WHERE state='committed'"
            parameters = ()
            if tenant_id is not None:
                sql += " AND tenant_id=?"
                parameters = (tenant_id,)
            rows = connection.execute(sql + " ORDER BY effect_key", parameters).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            connection.close()
