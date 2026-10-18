"""Public PDF → real OCR/vector index → actual OAuth/HTTP citation demo."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import platform
import re
import subprocess

from .integration import IdentityFixture, RagServer, IntegrationError
from .models import LocalEmbedder, verify_model
from .pdf_corpus import ingest_pdf_manifest
from .store import VectorStore


def extraction_runtime() -> dict:
    engine = subprocess.run(["tesseract", "--version"], capture_output=True, timeout=5, check=True)
    languages = subprocess.run(["tesseract", "--list-langs"], capture_output=True, timeout=5, check=True)
    listing = (languages.stdout + languages.stderr).decode("utf-8", errors="replace")
    match = re.search(r'"([^"\n]+)"', listing)
    traineddata_hash = None
    if match:
        traineddata = Path(match[1]) / "eng.traineddata"
        if traineddata.is_file() and traineddata.stat().st_size <= 50_000_000:
            traineddata_hash = hashlib.sha256(traineddata.read_bytes()).hexdigest()
    return {"python": platform.python_version(), "platform": platform.system(),
        "pypdf": version("pypdf"), "pypdfium2": version("pypdfium2"),
        "pillow": version("Pillow"),
        "tesseract": (engine.stdout + engine.stderr).decode().splitlines()[0],
        "english_traineddata_sha256": traineddata_hash,
        "ocr_language": "eng", "ocr_dpi": 180, "ocr_psm": 3}


def _assert_citations(body: dict, tenant: str, roles: set[str], pages: dict):
    for citation in body.get("citations", []):
        if citation["tenant_id"] != tenant or not roles.intersection(citation["allowed_roles"]):
            raise IntegrationError("PDF demo returned a forbidden citation.")
        page = pages[citation["document_id"]]
        span = citation["provenance"]
        if (page["text"][span["start"]:span["end"]] != citation["text"]
                or page["provenance"]["page_text_sha256"] != span["page_text_sha256"]
                or citation["quote"] not in citation["text"]):
            raise IntegrationError("PDF demo source span could not be resolved.")


def run_pdf_demo(*, manifest_path: Path, identity_dir: Path, model_dir: Path,
                 store_path: Path, out: Path, node: str) -> dict:
    runtime = extraction_runtime()
    runtime.update({name: version(name) for name in ("fastembed", "onnxruntime", "qdrant-client", "httpx")})
    node_version = subprocess.run([node, "--version"], capture_output=True, timeout=5, check=True).stdout.decode().strip()
    runtime["node"] = node_version
    embedding = verify_model(model_dir)
    store = VectorStore(store_path, LocalEmbedder(model_dir))
    try:
        ingestion = ingest_pdf_manifest(manifest_path, store)
        # Repeating the complete manifest must replace identical scoped IDs.
        before = store.client.count(store.collection_name, exact=True).count
        repeated = ingest_pdf_manifest(manifest_path, store)
        after = store.client.count(store.collection_name, exact=True).count
        if before != after or before != ingestion["chunks_indexed"] or ingestion != repeated:
            raise IntegrationError("PDF re-ingestion changed stable content or duplicated chunks.")
    finally:
        store.close()
    pages = {page["document_id"]: page for page in ingestion["pages"]}
    cases = []
    with IdentityFixture(identity_dir, node=node) as identity:
        server = RagServer(identity.issuer, model_dir, store_path)
        try:
            def check(name, client, tenant, question, expected, method=None, words=()):
                status, body = server.query(identity.token(client), tenant=tenant, query=question, top_k=5)
                roles = {"reader", "analyst"} if client.startswith("analyst") else {"reader"}
                if status != expected:
                    raise IntegrationError("PDF demo returned an unexpected HTTP status.")
                if status == 200:
                    _assert_citations(body, tenant, roles, pages)
                    if method and (body["abstained"] or not any(c["provenance"]["method"] == method for c in body["citations"])):
                        raise IntegrationError("PDF demo did not cite the expected extraction path.")
                    if any(word.lower() not in body["answer"].lower() for word in words):
                        raise IntegrationError("PDF demo answer did not preserve its expected source facts.")
                cases.append({"case": name, "client": client, "tenant": tenant,
                    "query": question, "status": status, "response": body})
                return body
            native = check("native-reader", "reader-alpha", "alpha",
                "How is access to individual enterprise resources granted?", 200, "native", ("per-session",))
            check("generic-ocr-question-observation", "analyst-alpha", "alpha",
                "What are the four functions?", 200)
            check("ocr-analyst", "analyst-alpha", "alpha", "What functions is the Core composed of?", 200, "ocr",
                ("GOVERN", "MAP", "MEASURE", "MANAGE"))
            denied = check("ocr-reader-denied", "reader-alpha", "alpha", "What functions is the Core composed of?", 200)
            if not denied["abstained"] or denied["citations"]:
                raise IntegrationError("Restricted OCR content escaped the role filter.")
            check("same-ocr-beta-reader", "reader-beta", "beta", "What functions is the Core composed of?", 200, "ocr",
                ("GOVERN", "MAP", "MEASURE", "MANAGE"))
            check("other-tenant-denied", "analyst-alpha", "beta", "What functions is the Core composed of?", 403)
            check("access-role-denied", "observer-alpha", "alpha", "What functions is the Core composed of?", 403)
            server.close()
            server = RagServer(identity.issuer, model_dir, store_path)
            persisted = check("restart-without-ingestion", "reader-alpha", "alpha",
                "How is access to individual enterprise resources granted?", 200, "native", ("per-session",))
            for key in ("answer", "abstained", "evidence"):
                if native[key] != persisted[key]:
                    raise IntegrationError("PDF persisted answer changed after resource restart.")
            def content(body):
                return [{k:v for k,v in c.items() if k != "score"} for c in body["citations"]]
            if content(native) != content(persisted):
                raise IntegrationError("PDF persisted provenance changed after resource restart.")
            check("ocr-after-restart", "analyst-alpha", "alpha", "What functions is the Core composed of?", 200, "ocr",
                ("GOVERN", "MAP", "MEASURE", "MANAGE"))
        finally:
            server.close()
    result = {"schema_version": 1, "recorded_at": datetime.now(timezone.utc).isoformat(),
        "runtime": runtime, "ingestion": ingestion,
        "idempotency": {"first_count": before, "second_count": after, "identical_page_text_and_metadata": True},
        "cases": cases, "generator": "extractive-v1", "embedding": embedding,
        "human_review_performed": False,
        "limitations": ["Two selected English pages; no OCR or answer-quality benchmark.",
            "OCR input is a labelled clean rasterized derivative, not a downloaded scan.",
            "Permissions are artificial trusted local grants, not NIST access restrictions.",
            "OCR confidence is diagnostic, not a calibrated probability."]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n")
    return result
