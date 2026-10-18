"""Approved, revision-fenced publication into the one local Qdrant owner.

SQLite and Qdrant do not share a transaction.  A FULL-synchronous pending journal
commits before index mutation and denies all reads/new effects until the same
immutable operation finishes.  Qdrant's own incomplete-write marker remains an
independent permanent fence: never delete either guard to recover availability.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
import time
from uuid import NAMESPACE_URL, uuid5

from temporalio import activity
from temporalio.exceptions import ApplicationError

from ..approval.contracts import ApprovalValidationError, format_utc
from ..rag.store import chunk_document
from .contracts import ApplyApprovedDocuments, PublicationEffect, validate_effect
from .storage import CandidateConflict, CandidateNotFound, CandidateRepository, canonical, document_dict, parse_document


MAX_PUBLISHED_CHUNKS = 5000


class PublicationConflict(ValueError):
    """CAS, document ownership or immutable effect identity conflicts."""


class PublicationBlocked(RuntimeError):
    """The local index cannot currently prove an entirely committed view."""


class DocumentPublisher:
    def __init__(self, candidates: CandidateRepository, store, *,
                 pause_after_commit_seconds: float = 0, fault_once_after_commit: bool = False):
        if (isinstance(pause_after_commit_seconds, bool)
                or not isinstance(pause_after_commit_seconds, (int, float))
                or not 0 <= pause_after_commit_seconds <= 15):
            raise ValueError("post-commit pause must be between 0 and 15 seconds")
        if not isinstance(fault_once_after_commit, bool):
            raise ValueError("invalid fault switch")
        self.candidates, self.store = candidates, store
        self.pause_after_commit_seconds = pause_after_commit_seconds
        self.fault_once_after_commit = fault_once_after_commit
        self.owner_path = self.store.path / "rag-publication-owner.json"
        with self.store.lock:
            self.store._open()
            self._bind()

    def _connect(self) -> sqlite3.Connection:
        connection = self.candidates._connect()
        connection.execute("""CREATE TABLE IF NOT EXISTS publication_config (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), store_path TEXT NOT NULL,
            owner_nonce TEXT NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS publication_tenants (
            tenant_id TEXT PRIMARY KEY, revision INTEGER NOT NULL)""")
        connection.execute("""CREATE TABLE IF NOT EXISTS publication_effects (
            effect_key TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, request_id TEXT NOT NULL,
            immutable_json TEXT NOT NULL, state TEXT NOT NULL,
            effect_json TEXT NOT NULL, committed_at TEXT,
            UNIQUE(tenant_id,request_id))""")
        connection.execute("""CREATE TABLE IF NOT EXISTS publication_documents (
            document_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
            document_json TEXT, candidate_sha256 TEXT NOT NULL)""")
        return connection

    def _bind(self) -> None:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            config = connection.execute("SELECT store_path,owner_nonce FROM publication_config WHERE singleton=1").fetchone()
            path = str(self.store.path.resolve())
            if config is None:
                # A lost/replaced ledger must never adopt an existing owned
                # index, and a first publisher must not adopt direct ingestion.
                if self.owner_path.exists() or self.store.client.count(self.store.collection_name, exact=True).count:
                    raise PublicationBlocked("index is not an empty unowned publication store")
                nonce = secrets.token_hex(32)
                self._write_owner({"schema": 1, "owner_nonce": nonce, "store_path": path})
                connection.execute("INSERT INTO publication_config VALUES (1,?,?)", (path, nonce))
            else:
                self._check_binding(config)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _write_owner(self, value: dict) -> None:
        with self.owner_path.open("x", encoding="utf-8") as stream:
            stream.write(canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(self.store.path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _check_binding(self, config) -> None:
        if config is None or len(config) != 2:
            raise PublicationBlocked("publication ledger ownership is missing")
        try:
            descriptor = os.open(self.owner_path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
            try:
                information = os.fstat(descriptor)
                if not stat.S_ISREG(information.st_mode) or information.st_size > 1024:
                    raise PublicationBlocked("invalid publication ownership marker")
                encoded = os.read(descriptor, 1025)
                if len(encoded) > 1024:
                    raise PublicationBlocked("invalid publication ownership marker")
                value = json.loads(encoded)
            finally:
                os.close(descriptor)
        except (OSError, ValueError) as exc:
            raise PublicationBlocked("publication ownership marker is missing or invalid") from exc
        expected = {"schema": 1, "owner_nonce": config[1], "store_path": config[0]}
        if value != expected or config[0] != str(self.store.path.resolve()):
            raise PublicationBlocked("publication ledger belongs to a different index")

    def _current_documents(self, connection) -> dict:
        result, candidates = {}, {}
        committed = set()
        for tenant_id, encoded in connection.execute("SELECT tenant_id,effect_json FROM publication_effects WHERE state='committed'"):
            try:
                effect = json.loads(encoded)
                if effect["tenant_id"] != tenant_id:
                    raise ValueError("effect tenant mismatch")
                committed.add((tenant_id, effect["candidate_sha256"]))
            except (ValueError, TypeError, KeyError) as exc:
                raise PublicationBlocked("invalid committed publication receipt") from exc
        for document_id, tenant_id, encoded, candidate_sha256 in connection.execute(
                "SELECT document_id,tenant_id,document_json,candidate_sha256 FROM publication_documents WHERE document_json IS NOT NULL"):
            try:
                document = parse_document(json.loads(encoded))
                if document.document_id != document_id or document.tenant_id != tenant_id:
                    raise ValueError("ledger document identity mismatch")
                key = (tenant_id, candidate_sha256)
                if key not in committed:
                    raise ValueError("document has no committed candidate receipt")
                if key not in candidates:
                    candidates[key] = {row.document_id: row for row in self.candidates.load_documents(*key)}
                staged = candidates[key].get(document_id)
                if staged is None or document_dict(staged) != document_dict(document):
                    raise ValueError("document differs from immutable committed candidate")
                result[document_id] = document
            except (ValueError, TypeError, CandidateNotFound) as exc:
                raise PublicationBlocked("invalid committed publication inventory") from exc
        return result

    @staticmethod
    def _expected_points(documents: dict) -> dict:
        chunks = [chunk for row in documents.values() for chunk in chunk_document(row)]
        if len(chunks) > MAX_PUBLISHED_CHUNKS:
            raise PublicationConflict("published inventory exceeds the local chunk limit")
        return {str(uuid5(NAMESPACE_URL, row["chunk_id"])): row for row in chunks}

    def _actual_points(self) -> dict:
        actual, cursor = {}, None
        while True:
            points, cursor = self.store.client.scroll(self.store.collection_name, offset=cursor,
                limit=256, with_payload=True, with_vectors=False)
            for point in points:
                actual[str(point.id)] = point.payload
            if len(actual) > MAX_PUBLISHED_CHUNKS:
                raise PublicationBlocked("local index exceeds the approved inventory limit")
            if cursor is None:
                return actual

    def assert_readable(self) -> None:
        """Call under store.lock for the entire subsequent RAG pipeline query."""
        with self.store.lock:
            self.store._open()
            connection = self._connect()
            try:
                self._check_binding(connection.execute("SELECT store_path,owner_nonce FROM publication_config WHERE singleton=1").fetchone())
                if connection.execute("SELECT 1 FROM publication_effects WHERE state!='committed' LIMIT 1").fetchone():
                    raise PublicationBlocked("unfinished approved publication; queries are fenced")
                expected = self._expected_points(self._current_documents(connection))
                if self._actual_points() != expected:
                    raise PublicationBlocked("vector inventory differs from committed approved documents")
            finally:
                connection.close()

    @activity.defn(name="publish_approved_documents")
    def publish_approved_documents(self, value: ApplyApprovedDocuments) -> PublicationEffect:
        try:
            validate_effect(value)
            documents = self.candidates.load_documents(value.request.tenant_id, value.request.candidate_sha256)
        except (ApprovalValidationError, CandidateConflict, CandidateNotFound) as exc:
            raise ApplicationError("invalid approved document candidate", type="CandidateConflict", non_retryable=True) from exc
        try:
            return self._publish(value, documents)
        except PublicationConflict as exc:
            raise ApplicationError("publication conflicts with current revision or immutable content",
                                   type="PublicationConflict", non_retryable=True) from exc

    def _publish(self, value: ApplyApprovedDocuments, documents) -> PublicationEffect:
        request = value.request
        effect_key = f"publication/{request.tenant_id}/{request.request_id}"
        immutable = canonical(asdict(value))
        chunk_count = sum(len(chunk_document(row)) for row in documents)
        effect = PublicationEffect(effect_key, request.tenant_id, request.request_id,
                                   request.snapshot_sha256, request.candidate_sha256,
                                   request.expected_revision + 1, len(documents), chunk_count)
        effect_json = canonical(asdict(effect))
        inserted = False
        with self.store.lock:
            self.store._open()
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                self._check_binding(connection.execute("SELECT store_path,owner_nonce FROM publication_config WHERE singleton=1").fetchone())
                prior = connection.execute("SELECT immutable_json,state,effect_json FROM publication_effects WHERE effect_key=?", (effect_key,)).fetchone()
                if prior is not None:
                    if prior[0] != immutable or prior[2] != effect_json:
                        raise PublicationConflict("effect key contains different immutable approval")
                    if prior[1] == "committed":
                        # Must precede CAS: an old successful delivery remains
                        # idempotent after another publication advances revision.
                        connection.commit()
                        self.assert_readable()
                        return effect
                    if prior[1] != "pending":
                        raise PublicationBlocked("invalid publication journal state")
                other = connection.execute("SELECT effect_key FROM publication_effects WHERE state!='committed' AND effect_key!=? LIMIT 1", (effect_key,)).fetchone()
                if other is not None:
                    raise PublicationBlocked("another unfinished publication owns the index fence")
                current_revision = connection.execute("SELECT revision FROM publication_tenants WHERE tenant_id=?", (request.tenant_id,)).fetchone()
                if (current_revision[0] if current_revision else 0) != request.expected_revision:
                    raise PublicationConflict("tenant revision changed since approval request")
                current = self._current_documents(connection)
                future = dict(current)
                for document in documents:
                    owner = connection.execute("SELECT tenant_id FROM publication_documents WHERE document_id=?", (document.document_id,)).fetchone()
                    if owner is not None and owner[0] != request.tenant_id:
                        raise PublicationConflict("document ID is reserved for another tenant")
                    future[document.document_id] = document
                expected, next_expected = self._expected_points(current), self._expected_points(future)
                actual = self._actual_points()
                if actual != expected and (prior is None or actual != next_expected):
                    raise PublicationBlocked("index differs from committed or exact pending inventory")
                # Token limits and model failures are preparation errors, not
                # partial writes. Prepare once before committing any fence, so
                # an oversized approved page cannot strand a healthy old index.
                with self.store._prepared_index(documents) as apply_index:
                    if prior is None:
                        connection.execute("INSERT INTO publication_effects VALUES (?,?,?,?,?,?,NULL)",
                            (effect_key, request.tenant_id, request.request_id, immutable, "pending", effect_json))
                        for document in documents:
                            connection.execute("INSERT INTO publication_documents VALUES (?,?,NULL,?) ON CONFLICT(document_id) DO NOTHING",
                                (document.document_id, request.tenant_id, request.candidate_sha256))
                    # The durable outer fence exists before Qdrant can change.
                    connection.commit()
                    apply_index()
                    if self._actual_points() != next_expected:
                        raise PublicationBlocked("completed vector mutation differs from approved candidate")
                    connection.execute("BEGIN IMMEDIATE")
                    # No newer publication can pass the pending journal, including
                    # after a process crash between these two independent stores.
                    for document in documents:
                        connection.execute("UPDATE publication_documents SET document_json=?,candidate_sha256=? WHERE document_id=? AND tenant_id=?",
                            (canonical(document_dict(document)), request.candidate_sha256, document.document_id, request.tenant_id))
                    connection.execute("INSERT INTO publication_tenants VALUES (?,?) ON CONFLICT(tenant_id) DO UPDATE SET revision=excluded.revision",
                                       (request.tenant_id, effect.revision))
                    connection.execute("UPDATE publication_effects SET state='committed',committed_at=? WHERE effect_key=? AND state='pending'",
                        (format_utc(datetime.now(timezone.utc)), effect_key))
                    connection.commit()
                    inserted = True
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        # This hook deliberately loses the activity acknowledgement after both
        # stores are committed. It is trusted CLI configuration, never HTTP data.
        if inserted and self.pause_after_commit_seconds:
            time.sleep(self.pause_after_commit_seconds)
        if inserted and self.fault_once_after_commit:
            raise RuntimeError("injected lost acknowledgement after approved publication commit")
        return effect
