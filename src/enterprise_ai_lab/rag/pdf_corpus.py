"""Trusted local ingestion grants. PDF contents never choose tenant or roles."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import stat

from .contracts import Document
from .ingestion import extract_pdf
from .provenance import SourceSpan, WordBox

FIELDS = {"source_id", "source_uri", "source_sha256", "source_path", "artifact_path",
          "artifact_sha256", "pages", "ocr_pages", "tenant_id", "allowed_roles", "title"}


def _read_regular(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("ingestion inputs must be regular local files")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("ingestion input exceeds byte limit")
    return data


def _unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate ingestion manifest key")
        value[key] = item
    return value


def _local_file(root: Path, name: str, digest: str) -> Path:
    if not isinstance(name, str) or Path(name).is_absolute():
        raise ValueError("PDF paths must be relative to the manifest")
    path = (root / name).resolve(strict=True)
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError("PDF must be contained in the local manifest directory")
    data = _read_regular(path, 15_000_000)
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("PDF bytes differ from the pinned manifest")
    return path


def _load(manifest_path: Path) -> tuple[list[Document], dict]:
    manifest_path = Path(manifest_path).resolve(strict=True)
    raw = _read_regular(manifest_path, 512_000)
    manifest = json.loads(raw, object_pairs_hook=_unique)
    if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "entries"}
            or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
            or not isinstance(manifest["entries"], list) or not 1 <= len(manifest["entries"]) <= 32):
        raise ValueError("invalid ingestion manifest")
    documents, pages = [], []
    seen = set()
    for entry in manifest["entries"]:
        if not isinstance(entry, dict) or set(entry) != FIELDS:
            raise ValueError("invalid ingestion grant schema")
        if (not isinstance(entry["pages"], list) or not entry["pages"] or len(entry["pages"]) > 12
                or not isinstance(entry["ocr_pages"], list)
                or not isinstance(entry["allowed_roles"], list)):
            raise ValueError("invalid explicit page/role selection")
        mappings = {}
        for mapping in entry["pages"]:
            if (not isinstance(mapping, dict) or set(mapping) != {"artifact_page", "source_page"}
                    or any(type(v) is not int or v < 1 for v in mapping.values())
                    or mapping["artifact_page"] in mappings):
                raise ValueError("invalid source page mapping")
            mappings[mapping["artifact_page"]] = mapping["source_page"]
        # Validate identity, title and ACL before touching a parser.
        Document("grant-check", entry["tenant_id"], tuple(entry["allowed_roles"]), entry["title"], "validation")
        root = manifest_path.parent
        source = _local_file(root, entry["source_path"], entry["source_sha256"])
        artifact = _local_file(root, entry["artifact_path"], entry["artifact_sha256"])
        # Page existence is checked by the same bounded worker. Visual/content
        # correspondence of a derivative is a reviewable operator assertion.
        same_bytes = entry["source_sha256"] == entry["artifact_sha256"]
        if same_bytes and any(k != v for k, v in mappings.items()):
            raise ValueError("unmodified source must preserve original page numbers")
        if not same_bytes:
            original_pages = extract_pdf(source, source_id=entry["source_id"], source_uri=entry["source_uri"],
                pages=tuple(dict.fromkeys(mappings.values())))
            if any(page.source_sha256 != entry["source_sha256"] for page in original_pages):
                raise ValueError("original PDF changed during ingestion")
        extracted = extract_pdf(artifact, source_id=entry["source_id"], source_uri=entry["source_uri"],
            pages=tuple(mappings), ocr_pages=tuple(entry["ocr_pages"]))
        for page in extracted:
            if page.source_sha256 != entry["artifact_sha256"]:
                raise ValueError("PDF changed during ingestion")
            identity = [entry["tenant_id"], entry["source_id"], mappings[page.page]]
            document_id = "pdf-" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:40]
            if document_id in seen:
                raise ValueError("duplicate source page grant; combine roles explicitly")
            seen.add(document_id)
            span = SourceSpan(entry["source_id"], entry["source_uri"], entry["source_sha256"],
                mappings[page.page], page.source_sha256, page.page, page.method,
                page.page_text_sha256, 0, len(page.text),
                tuple(WordBox(**asdict(word)) for word in page.ocr_words),
                page.render_dpi, page.width_px, page.height_px)
            document = Document(document_id, entry["tenant_id"], tuple(entry["allowed_roles"]),
                entry["title"], page.text, span)
            documents.append(document)
            pages.append({"document_id": document_id, "tenant_id": document.tenant_id,
                "allowed_roles": list(document.allowed_roles), "text": page.text,
                "provenance": span.as_dict()})
            if len(documents) > 100:
                raise ValueError("ingestion exceeds 100 selected page grants")
    return documents, {"schema_version": 1, "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "pages": pages, "permissions_from": "trusted-local-manifest", "network_fetches": 0}


def load_pdf_documents(manifest_path: Path) -> list[Document]:
    return _load(manifest_path)[0]


def ingest_pdf_manifest(manifest_path: Path, store) -> dict:
    """Extract the complete batch before one guarded index mutation.

    Re-ingestion replaces these same tenant/source/page IDs, including ACLs.
    Omitted pages are not deletion requests; remove them explicitly with the
    store's guarded delete operation. This is an operator tool, not an upload API.
    """
    documents, report = _load(manifest_path)
    report["chunks_indexed"] = store.index(documents)
    report["page_grants"] = len(documents)
    return report
