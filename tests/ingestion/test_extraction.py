"""Real PDF parsing/rendering/Tesseract tests; the authored fixtures are synthetic."""
from __future__ import annotations

from dataclasses import replace
from contextlib import closing
import hashlib
from pathlib import Path
import shutil

import pypdfium2
from pypdf import PdfReader, PdfWriter
import pytest
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from enterprise_ai_lab.rag.ingestion import (
    ExtractionError, ExtractionLimits, _parse_tsv, extract_pdf,
)


SOURCE = {"source_id": "controlled-pdf", "source_uri": "https://example.org/controlled.pdf"}


def native_pdf(path: Path, texts=("Tenant alpha permits analysts to inspect the public research.",)) -> Path:
    canvas = Canvas(str(path), pagesize=(612, 792), invariant=1)
    for text in texts:
        if text:
            canvas.setFont("Helvetica", 16)
            canvas.drawString(48, 700, text)
        canvas.showPage()
    canvas.save()
    return path


def scanned_pdf(path: Path, text="Public research records require source citations.") -> Path:
    # First render a controlled native page, then embed only its pixels. No text
    # layer remains; the extraction assertion therefore requires actual OCR.
    original = native_pdf(path.with_name("before-raster.pdf"), (text,))
    with pypdfium2.PdfDocument(original) as document:
        with closing(document.get_page(0)) as page:
            with closing(page.render(scale=2)) as bitmap:
                image = bitmap.to_pil()
                try:
                    canvas = Canvas(str(path), pagesize=(612, 792), invariant=1)
                    canvas.drawImage(ImageReader(image), 0, 0, width=612, height=792)
                    canvas.showPage()
                    canvas.save()
                finally:
                    image.close()
    assert not PdfReader(path).pages[0].extract_text().strip()
    return path


def test_native_pages_have_exact_input_hash_and_canonical_text_hash(tmp_path):
    path = native_pdf(tmp_path / "native.pdf", ("First source text.", "Second source text."))
    pages = extract_pdf(path, **SOURCE, pages=(2, 1))
    assert [page.page for page in pages] == [2, 1]
    assert [page.text for page in pages] == ["Second source text.", "First source text."]
    for page in pages:
        assert page.source_id == SOURCE["source_id"]
        assert page.source_uri == SOURCE["source_uri"]
        assert page.source_sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
        assert page.page_text_sha256 == hashlib.sha256(page.text.encode()).hexdigest()
        assert page.method == "native"
        assert page.ocr_words == ()
        assert page.render_dpi is page.width_px is page.height_px is None


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="requires real Tesseract 5 with English data")
def test_image_only_page_runs_actual_tesseract_and_retains_each_word_span(tmp_path):
    path = scanned_pdf(tmp_path / "scan.pdf")
    page, = extract_pdf(path, **SOURCE, pages=(1,))
    assert page.method == "ocr"
    assert "Public research records require source citations." in page.text
    assert page.render_dpi == 180
    assert (page.width_px, page.height_px) == (1530, 1980)
    assert page.ocr_words
    for word in page.ocr_words:
        assert page.text[word.start:word.end] == word.text
        assert 0 <= word.confidence <= 100
        assert 0 <= word.left < word.left + word.width <= page.width_px
        assert 0 <= word.top < word.top + word.height <= page.height_px
    assert page.page_text_sha256 == hashlib.sha256(page.text.encode()).hexdigest()


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="requires real Tesseract")
def test_manifest_can_force_ocr_even_when_native_text_exists(tmp_path):
    path = native_pdf(tmp_path / "native.pdf", ("Permission checks happen before retrieval.",))
    page, = extract_pdf(path, **SOURCE, pages=(1,), ocr_pages=(1,))
    assert page.method == "ocr"
    assert "Permission checks happen before retrieval." in page.text
    assert page.ocr_words


def test_same_bytes_are_deterministic_for_native_extraction(tmp_path):
    path = native_pdf(tmp_path / "native.pdf")
    first = extract_pdf(path, **SOURCE, pages=(1,))
    second = extract_pdf(path, **SOURCE, pages=(1,))
    assert first == second


def test_encrypted_pdf_is_rejected_even_with_empty_user_password(tmp_path):
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("", owner_password="owner-password")
    path = tmp_path / "encrypted.pdf"
    writer.write(path)
    with pytest.raises(ExtractionError, match="encrypted") as error:
        extract_pdf(path, **SOURCE, pages=(1,))
    assert error.value.code == "encrypted_pdf"


def test_corrupt_pdf_is_rejected(tmp_path):
    path = tmp_path / "corrupt.pdf"
    path.write_bytes(b"%PDF-1.7\nnot a PDF body\n%%EOF\n")
    with pytest.raises(ExtractionError) as error:
        extract_pdf(path, **SOURCE, pages=(1,))
    assert error.value.code == "invalid_pdf"


def test_non_pdf_bytes_are_rejected(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"not a PDF")
    with pytest.raises(ExtractionError, match="not a PDF"):
        extract_pdf(path, **SOURCE, pages=(1,))


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="requires real Tesseract")
def test_blank_selected_page_rejects_whole_extraction(tmp_path):
    path = native_pdf(tmp_path / "partial.pdf", ("This first page is readable.", ""))
    with pytest.raises(ExtractionError, match="page 2 has no extractable text") as error:
        extract_pdf(path, **SOURCE, pages=(1, 2))
    assert error.value.code == "empty_page"


def test_selection_outside_real_document_is_rejected(tmp_path):
    path = native_pdf(tmp_path / "native.pdf")
    with pytest.raises(ExtractionError, match="outside") as error:
        extract_pdf(path, **SOURCE, pages=(1, 2))
    assert error.value.code == "invalid_selection"


def test_total_pdf_page_count_is_bounded_even_when_selecting_one_page(tmp_path):
    path = native_pdf(tmp_path / "native.pdf", ("One.", "Two."))
    with pytest.raises(ExtractionError, match="page count") as error:
        extract_pdf(path, **SOURCE, pages=(1,), limits=replace(ExtractionLimits(), max_pdf_pages=1))
    assert error.value.code == "resource_limit"


@pytest.mark.parametrize("field,value,message", [
    ("max_file_bytes", 100, "file byte"),
    ("max_page_content_bytes", 10, "decoded page content"),
    ("max_page_chars", 5, "page text"),
    ("max_total_chars", 5, "total character"),
])
def test_native_resource_limits(tmp_path, field, value, message):
    path = native_pdf(tmp_path / "native.pdf")
    with pytest.raises(ExtractionError, match=message) as error:
        extract_pdf(path, **SOURCE, pages=(1,), limits=replace(ExtractionLimits(), **{field: value}))
    assert error.value.code == "resource_limit"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="requires real Tesseract")
@pytest.mark.parametrize("field,value,message", [
    ("max_page_pixels", 1000, "pixel limit"),
    ("max_ocr_words", 1, "word count"),
])
def test_actual_ocr_resource_limits(tmp_path, field, value, message):
    path = scanned_pdf(tmp_path / "scan.pdf")
    with pytest.raises(ExtractionError, match=message) as error:
        extract_pdf(path, **SOURCE, pages=(1,), limits=replace(ExtractionLimits(), **{field: value}))
    assert error.value.code == "resource_limit"


TSV_HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def test_tsv_offsets_address_canonical_page_text_and_confidence_is_preserved():
    text, words = _parse_tsv(TSV_HEADER + (
        "5\t1\t1\t1\t1\t1\t2\t3\t10\t5\t0.0\talpha\n"
        "5\t1\t1\t1\t1\t2\t15\t3\t10\t5\t91.25\tbeta\n"
        "5\t1\t1\t1\t2\t1\t2\t15\t20\t5\t100\tomega\n"
    ), 100, 100, 3)
    assert text == "alpha beta\nomega"
    assert [(word.start, word.end) for word in words] == [(0, 5), (6, 10), (11, 16)]
    assert [word.confidence for word in words] == [0.0, 91.25, 100.0]
    assert all(text[word.start:word.end] == word.text for word in words)


@pytest.mark.parametrize("fields", [
    "5\t1\t1\t1\t1\t1\t-1\t3\t10\t5\t90\tword",
    "5\t1\t1\t1\t1\t1\t95\t3\t10\t5\t90\tword",
    "5\t1\t1\t1\t1\t1\t2\t99\t10\t5\t90\tword",
    "5\t1\t1\t1\t1\t1\t2\t3\t0\t5\t90\tword",
    "5\t1\t1\t1\t1\t1\t2\t3\t10\t5\tNaN\tword",
    "5\t1\t1\t1\t1\t1\t2\t3\t10\t5\t101\tword",
    "5\t1\t1\t1\t1\t1\t2\t3\t10\t5\t-1\tword",
    "5\t1\t1\t1\t1\t1\t2\t3\t10\t5\t90\tword\ttab",
])
def test_invalid_tsv_geometry_or_confidence_is_rejected(fields):
    with pytest.raises(ExtractionError) as error:
        _parse_tsv(TSV_HEADER + fields + "\n", 100, 100, 10)
    assert error.value.code == "ocr_failure"


def test_missing_tsv_columns_are_rejected():
    with pytest.raises(ExtractionError, match="TSV row"):
        _parse_tsv(TSV_HEADER + "5\t1\tword\n", 100, 100, 10)


def test_unknown_tsv_header_is_rejected():
    with pytest.raises(ExtractionError, match="TSV header"):
        _parse_tsv("not a TSV", 100, 100, 10)


@pytest.mark.parametrize("changes", [
    {"dpi": 71}, {"dpi": 301}, {"dpi": True},
    {"max_file_bytes": 0}, {"max_pdf_pages": -1}, {"max_page_pixels": 2.5},
    {"ocr_timeout_seconds": 0}, {"ocr_timeout_seconds": float("nan")},
    {"process_timeout_seconds": float("inf")}, {"process_timeout_seconds": True},
])
def test_extraction_limits_reject_invalid_operator_configuration(changes):
    with pytest.raises(ValueError):
        ExtractionLimits(**changes)
