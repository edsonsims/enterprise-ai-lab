"""Persistent Qdrant local-mode vector search with mandatory tenant AND role filters."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
from uuid import NAMESPACE_URL, uuid5

from qdrant_client import QdrantClient, models
from .provenance import SourceSpan
from .contracts import Document, EmbeddingLimitError, QueryLimitError, RagPrincipal, RetrievedChunk, valid_identifier


class UnsafePayloadError(RuntimeError):
    """A result violated the policy/schema; return no context from this query."""


def chunk_document(document: Document) -> list[dict]:
    """Paragraph boundaries, then <=500-character pieces; chunks never cross documents."""
    pieces = []
    boundaries = [0]
    for separator in re.finditer(r"\n\s*\n", document.text):
        boundaries.extend((separator.start(), separator.end()))
    boundaries.append(len(document.text))
    for begin, finish in zip(boundaries[::2], boundaries[1::2]):
        while begin < finish and document.text[begin].isspace():
            begin += 1
        while finish > begin and document.text[finish - 1].isspace():
            finish -= 1
        while begin < finish:
            end = min(begin + 500, finish)
            if end < finish:
                # Keep complete evidence sentences when possible without
                # changing canonical text or the original synthetic study.
                sentences = list(re.finditer(r"[.!?](?=\s)", document.text[begin:end])) if document.provenance else []
                boundary = begin + sentences[-1].end() if sentences and sentences[-1].end() >= 100 else document.text.rfind(" ", begin, end)
                if boundary > begin:
                    end = boundary
            pieces.append((begin, end))
            begin = end
            while begin < finish and document.text[begin].isspace():
                begin += 1
    chunks = []
    for index, (begin, end) in enumerate(pieces):
        text = document.text[begin:end]
        chunk = {"document_id": document.document_id,
                 "chunk_id": f"{document.document_id}:{index:03d}:{hashlib.sha256(text.encode()).hexdigest()[:12]}",
                 "tenant_id": document.tenant_id, "allowed_roles": list(document.allowed_roles),
                 "title": document.title, "text": text}
        if document.provenance is not None:
            chunk["provenance"] = document.provenance.slice(
                document.provenance.start + begin, document.provenance.start + end).as_dict()
        chunks.append(chunk)
    return chunks


class VectorStore:
    """One process owns a local directory; an RLock serializes readers and ACL updates.

    This uses the actual Qdrant local engine, not a simulated list of scores.
    Local mode is an embedded test/development backend; it is not a distributed server.
    """
    def __init__(self, path, embedder, collection_name="documents"):
        if not valid_identifier(collection_name):
            raise ValueError("invalid collection name")
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self._pending_path = self.path / "rag-write-pending"
        if self._pending_path.exists():
            raise ValueError("incomplete local index mutation; rebuild in a fresh index directory")
        self.embedder = embedder
        self.collection_name = collection_name
        self.lock = RLock()
        self._closed = False
        fingerprint = {"schema_version": 2, "collection": collection_name,
                       "model_sha256": embedder.fingerprint,
                       "dimensions": embedder.dimensions, "distance": "Cosine", "chunk_chars": 500,
                       "pdf_chunking": "sentence-boundary-v1"}
        manifest_path = self.path / "rag-index.json"
        if manifest_path.exists():
            try:
                previous = json.loads(manifest_path.read_text())
            except (ValueError, OSError) as exc:
                raise ValueError("invalid vector index manifest") from exc
            if previous != fingerprint:
                raise ValueError("index embedding/config fingerprint differs; use a fresh index directory")
        self.client = QdrantClient(path=str(self.path), force_disable_check_same_thread=True)
        try:
            if self.client.collection_exists(collection_name):
                if not manifest_path.exists():
                    raise ValueError("existing collection lacks a verified index manifest")
                params = self.client.get_collection(collection_name).config.params.vectors
                if params.size != embedder.dimensions or params.distance != models.Distance.COSINE:
                    raise ValueError("vector collection configuration differs")
            else:
                self.client.create_collection(collection_name,
                    vectors_config=models.VectorParams(size=embedder.dimensions, distance=models.Distance.COSINE))
            if not manifest_path.exists():
                temporary = manifest_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(fingerprint, indent=2) + "\n")
                temporary.replace(manifest_path)
        except BaseException:
            self.client.close()
            raise

    def _open(self):
        if self._closed:
            raise RuntimeError("vector store is closed")
        if self._pending_path.exists():
            raise RuntimeError("incomplete local index mutation; rebuild in a fresh index directory")

    @contextmanager
    def _mutation(self):
        """Qdrant local writes persist per point, not as one bulk transaction.

        A durable marker makes ANY incomplete bulk mutation deny subsequent reads
        and startup. Rebuild a fresh index rather than guessing at a partial ACL.
        """
        with self._pending_path.open("x", encoding="utf-8") as stream:
            stream.write("Unfinished local mutation: rebuild a fresh index directory.\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(self.path, os.O_RDONLY)
        try:
            os.fsync(directory)
            yield
            self._pending_path.unlink()
            os.fsync(directory)
        finally:
            os.close(directory)

    @contextmanager
    def _prepared_index(self, documents):
        """Prepare all embeddings before an outer publication journal commits.

        The single-use closure keeps prepared points private to this locked
        context. Callers cannot supply a forged batch or apply it after the lock
        has been released. The original guarded mutation remains unchanged.
        """
        documents = list(documents)
        if not documents or len(documents) > 1000 or any(not isinstance(row, Document) for row in documents):
            raise ValueError("index needs 1 to 1000 validated documents")
        if len({row.document_id for row in documents}) != len(documents):
            raise ValueError("duplicate document IDs")
        chunks = [chunk for document in documents for chunk in chunk_document(document)]
        if len(chunks) > 2000:
            raise ValueError("index operation exceeds 2000 chunks")
        with self.lock:
            self._open()
            # Compute all vectors before mutating the index, so malformed/oversized
            # input cannot partially replace existing content.
            vectors = self.embedder.embed([chunk["text"] for chunk in chunks])
            points = [models.PointStruct(id=str(uuid5(NAMESPACE_URL, chunk["chunk_id"])),
                       vector=vector, payload=chunk) for chunk, vector in zip(chunks, vectors)]
            active, applied = True, False

            def apply():
                nonlocal applied
                if not active or applied:
                    raise RuntimeError("prepared index mutation is no longer available")
                applied = True
                self._open()
                with self._mutation():
                    # Delete old ACL-bearing chunks first. A failed delete/upsert
                    # leaves the durable marker, so even a restart cannot serve stale ACLs.
                    self.client.delete(self.collection_name, models.FilterSelector(filter=models.Filter(must=[
                        models.FieldCondition(key="document_id", match=models.MatchAny(any=[row.document_id for row in documents]))])), wait=True)
                    self.client.upsert(self.collection_name, points=points, wait=True)
                return len(points)

            try:
                yield apply
            finally:
                active = False

    def index(self, documents) -> int:
        with self._prepared_index(documents) as apply:
            return apply()

    def search(self, question: str, principal: RagPrincipal, top_k: int = 3) -> list[RetrievedChunk]:
        if not isinstance(principal, RagPrincipal):
            raise ValueError("search needs a verified RagPrincipal")
        if not isinstance(question, str) or not question.strip() or len(question) > 2000:
            raise QueryLimitError("question must contain 1 to 2000 characters")
        if type(top_k) is not int or not 1 <= top_k <= 5:
            raise QueryLimitError("top_k must be an integer from 1 to 5")
        with self.lock:
            self._open()
            try:
                vector = self.embedder.embed([question])[0]
            except EmbeddingLimitError as exc:
                raise QueryLimitError(str(exc)) from exc
            # Both clauses are passed into the engine BEFORE ranking/limit.
            policy = models.Filter(must=[
                models.FieldCondition(key="tenant_id", match=models.MatchValue(value=principal.tenant_id)),
                models.FieldCondition(key="allowed_roles", match=models.MatchAny(any=list(principal.roles))),
            ])
            points = self.client.query_points(self.collection_name, query=vector,
                query_filter=policy, limit=top_k, score_threshold=0.35,
                with_payload=True, with_vectors=False).points
            chunks = []
            for point in points:
                payload = point.payload or {}
                try:
                    valid = (set(payload) in ({"document_id", "chunk_id", "tenant_id", "allowed_roles", "title", "text"},
                            {"document_id", "chunk_id", "tenant_id", "allowed_roles", "title", "text", "provenance"})
                        and valid_identifier(payload["document_id"])
                        and isinstance(payload["chunk_id"], str) and 1 <= len(payload["chunk_id"]) <= 140
                        and payload["tenant_id"] == principal.tenant_id
                        and isinstance(payload["allowed_roles"], list) and bool(payload["allowed_roles"])
                        and all(valid_identifier(role) for role in payload["allowed_roles"])
                        and len(set(payload["allowed_roles"])) == len(payload["allowed_roles"])
                        and bool(set(payload["allowed_roles"]) & set(principal.roles))
                        and isinstance(payload["text"], str) and 1 <= len(payload["text"]) <= 500
                        and isinstance(payload["title"], str) and 1 <= len(payload["title"]) <= 200
                        and type(point.score) in (int, float) and math.isfinite(point.score))
                    provenance = None
                    if valid:
                        payload["text"].encode("utf-8")
                        if "provenance" in payload:
                            provenance = SourceSpan.from_dict(payload["provenance"])
                            provenance.validate_text(payload["text"])
                except (KeyError, TypeError, ValueError, UnicodeError):
                    valid = False
                if not valid:
                    raise UnsafePayloadError("vector result failed authorization or schema checks")
                chunks.append(RetrievedChunk(payload["document_id"], payload["chunk_id"], payload["text"],
                    float(point.score), payload["tenant_id"], tuple(payload["allowed_roles"]), provenance))
            return chunks

    def update_acl(self, document_id, tenant_id, allowed_roles):
        # Reconstruct a validated ACL without allowing a tenant move.
        if not isinstance(allowed_roles, (list, tuple)):
            raise ValueError("allowed_roles must be an explicit list or tuple")
        Document(document_id, tenant_id, tuple(allowed_roles), "ACL validation", "ACL validation")
        with self.lock:
            self._open()
            with self._mutation():
                self.client.set_payload(self.collection_name,
                    payload={"allowed_roles": list(allowed_roles)}, points=models.Filter(must=[
                        models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id)),
                        models.FieldCondition(key="tenant_id", match=models.MatchValue(value=tenant_id)),
                    ]), wait=True)

    def delete_document(self, document_id):
        if not valid_identifier(document_id):
            raise ValueError("invalid document ID")
        with self.lock:
            self._open()
            with self._mutation():
                self.client.delete(self.collection_name, models.FilterSelector(filter=models.Filter(must=[
                    models.FieldCondition(key="document_id", match=models.MatchValue(value=document_id))])), wait=True)

    def close(self):
        with self.lock:
            if not self._closed:
                self.client.close()
                self._closed = True
