"""Independent source/artifact identity regressions using real public PDFs.

The file replacement below is a controlled local race. Extraction still runs
the real parser; no fabricated page text or source hash is returned by a double.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import Mock, patch

import pytest

from enterprise_ai_lab.rag.ingestion import extract_pdf
from enterprise_ai_lab.rag.pdf_corpus import ingest_pdf_manifest, load_pdf_documents


ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ROOT / "public-data/pdf-v1"


def grant(directory: Path, *, source_name: str, artifact_name: str,
          source_page: int, artifact_page: int) -> Path:
    value = {
        "source_id": "public-source-review",
        "source_uri": "https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.100-1.pdf",
        "source_sha256": hashlib.sha256((directory / source_name).read_bytes()).hexdigest(),
        "source_path": source_name,
        "artifact_sha256": hashlib.sha256((directory / artifact_name).read_bytes()).hexdigest(),
        "artifact_path": artifact_name,
        "pages": [{"artifact_page": artifact_page, "source_page": source_page}],
        "ocr_pages": [], "tenant_id": "alpha", "allowed_roles": ["reader"],
        "title": "Public PDF source identity regression",
    }
    path = directory / "ingestion.json"
    path.write_text(json.dumps({"schema_version": 1, "entries": [value]}))
    return path


def test_equal_pdf_bytes_cannot_remap_pages_through_a_different_filename(tmp_path):
    original = PUBLIC / "nist-sp-800-207.pdf"
    shutil.copyfile(original, tmp_path / "source.pdf")
    shutil.copyfile(original, tmp_path / "alias.pdf")
    manifest = grant(tmp_path, source_name="source.pdf", artifact_name="alias.pdf",
                     source_page=14, artifact_page=15)
    # Before the fix, the alias bypassed path equality and page 15's per-session
    # fact was falsely attributed to original page 14 despite equal PDF hashes.
    with pytest.raises(ValueError, match="preserve original page numbers"):
        load_pdf_documents(manifest)


def test_replaced_original_snapshot_hash_must_match_before_any_index_mutation(tmp_path):
    source = tmp_path / "source.pdf"
    shutil.copyfile(PUBLIC / "nist-ai-100-1.pdf", source)
    shutil.copyfile(PUBLIC / "nist-ai-100-1-page25-raster.pdf", tmp_path / "raster.pdf")
    manifest = grant(tmp_path, source_name="source.pdf", artifact_name="raster.pdf",
                     source_page=50, artifact_page=1)
    # The pinned AI RMF original has 48 pages. Replace its path after local hash
    # validation with another real public PDF that has 59 pages. The old code
    # checked page existence against the replacement, ignored its measured hash,
    # and emitted a citation to impossible page 50 of the pinned 48-page source.
    calls = []

    def replace_original_then_extract(path, **kwargs):
        calls.append(Path(path))
        if Path(path) == source:
            shutil.copyfile(PUBLIC / "nist-sp-800-207.pdf", source)
        return extract_pdf(path, **kwargs)

    store = Mock()
    with patch("enterprise_ai_lab.rag.pdf_corpus.extract_pdf", side_effect=replace_original_then_extract):
        with pytest.raises(ValueError, match="original PDF changed during ingestion"):
            ingest_pdf_manifest(manifest, store)
    assert calls == [source]
    store.index.assert_not_called()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX regular-file boundary")
def test_fifo_manifest_is_rejected_without_waiting_for_a_writer(tmp_path):
    path = tmp_path / "manifest.json"
    os.mkfifo(path)
    script = (
        "import sys; from enterprise_ai_lab.rag.pdf_corpus import load_pdf_documents\n"
        "try: load_pdf_documents(sys.argv[1])\n"
        "except ValueError as error: assert 'regular' in str(error)\n"
        "else: raise AssertionError('FIFO manifest was accepted')\n"
    )
    # Run in a child so the pre-fix blocking open produces a bounded test
    # failure instead of hanging the entire test worker.
    completed = subprocess.run([sys.executable, "-c", script, str(path)],
                               capture_output=True, text=True, timeout=5)
    assert completed.returncode == 0, completed.stderr
