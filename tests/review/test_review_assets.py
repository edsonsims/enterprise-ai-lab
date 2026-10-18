"""Actual public PDF/OCR previews, not an upload-sandbox or OCR quality score."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
import hashlib
import math
import os
from pathlib import Path
import threading

from PIL import Image, ImageChops
from pypdf import PdfReader
import pytest

from enterprise_ai_lab.rag.pdf_corpus import load_pdf_documents
from enterprise_ai_lab.review.assets import PdfAssets

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "public-data/pdf-v1/ingestion.json"


@pytest.fixture(scope="module")
def public_documents():
    # This calls actual pypdf plus PDFium -> system Tesseract for the labelled
    # raster derivative. The source rights remain in public-data/pdf-v1.
    documents = load_pdf_documents(MANIFEST)
    return [document for document in documents if document.tenant_id == "alpha"]


@pytest.mark.parametrize("method,variant,physical,original_count", [
    ("native", "artifact", 15, 59), ("native", "original", 15, 59),
    ("ocr", "artifact", 1, 48), ("ocr", "original", 25, 48),
])
def test_real_pdf_preview_contains_only_the_authorized_physical_page(public_documents, method, variant, physical, original_count):
    assets = PdfAssets(MANIFEST)
    document = next(row for row in public_documents if row.provenance.method == method)
    data, metadata = assets.preview(document, variant=variant)
    preview = PdfReader(BytesIO(data), strict=True)
    entry, mapping, source, artifact = assets.entries[(document.tenant_id, document.document_id)]
    assert len(PdfReader(source).pages) == original_count
    assert len(preview.pages) == metadata["page_count"] == 1
    assert metadata["selected_physical_page"] == physical
    assert metadata["variant"] == variant
    assert metadata["preview_sha256"] == hashlib.sha256(data).hexdigest()
    assert metadata["original_sha256"] == document.provenance.source_sha256
    assert metadata["artifact_sha256"] == document.provenance.artifact_sha256
    expected_path = source if variant == "original" else artifact
    expected = PdfReader(expected_path).pages[physical - 1].extract_text()
    assert preview.pages[0].extract_text() == expected
    assert "/Annots" not in preview.pages[0] and "/AA" not in preview.pages[0]
    assert "preview derivative" in preview.metadata.subject
    if method == "ocr" and variant == "artifact":
        assert document.provenance.ocr_words
        assert not preview.pages[0].extract_text().strip()  # Actual image-only fixture.


@pytest.mark.parametrize("field,value", [
    ("source_uri", "https://example.org/wrong.pdf"),
    ("source_id", "wrong-source"), ("source_sha256", "1" * 64),
    ("artifact_sha256", "2" * 64), ("source_page", 26), ("artifact_page", 2),
])
def test_preview_requires_exact_candidate_source_mapping(public_documents, field, value):
    assets = PdfAssets(MANIFEST)
    document = next(row for row in public_documents if row.provenance.method == "ocr")
    altered = replace(document, provenance=replace(document.provenance, **{field: value}))
    with pytest.raises(ValueError, match="source differs"):
        assets.preview(altered)


@pytest.mark.parametrize("variant", ["all", "../original", "", None])
def test_preview_has_no_whole_document_or_path_selector(public_documents, variant):
    with pytest.raises(ValueError):
        PdfAssets(MANIFEST).preview(public_documents[0], variant=variant)


def test_changed_regular_snapshot_refuses_preview(public_documents, tmp_path):
    assets = PdfAssets(MANIFEST)
    document = public_documents[0]
    key = (document.tenant_id, document.document_id)
    entry, mapping, source, artifact = assets.entries[key]
    changed = tmp_path / "changed.pdf"
    changed.write_bytes(source.read_bytes() + b"changed")
    assets.entries[key] = (entry, mapping, changed, artifact)
    with pytest.raises(ValueError, match="snapshot changed"):
        assets.preview(document)


def test_fifo_and_symlink_replacement_are_refused_before_parser(public_documents, tmp_path):
    document = public_documents[0]
    assets = PdfAssets(MANIFEST)
    key = (document.tenant_id, document.document_id)
    entry, mapping, source, artifact = assets.entries[key]
    fifo = tmp_path / "fifo.pdf"
    os.mkfifo(fifo)
    assets.entries[key] = (entry, mapping, fifo, artifact)
    with pytest.raises(ValueError, match="regular"):
        assets.preview(document)
    link = tmp_path / "linked.pdf"
    link.symlink_to(source)
    assets.entries[key] = (entry, mapping, link, artifact)
    with pytest.raises(OSError):
        assets.preview(document)


def test_preview_refuses_unmapped_document_and_wrong_tenant(public_documents):
    assets = PdfAssets(MANIFEST)
    document = public_documents[0]
    with pytest.raises(LookupError):
        assets.preview(replace(document, document_id="unmapped-page"))
    with pytest.raises(LookupError):
        assets.preview(replace(document, tenant_id="unmapped-tenant"))


@pytest.mark.parametrize("method,variant,physical", [
    ("native", "artifact", 15), ("native", "original", 15),
    ("ocr", "artifact", 1), ("ocr", "original", 25),
])
def test_actual_page_png_has_bounded_nonwhite_pixels_and_exact_source_binding(public_documents, method, variant, physical):
    assets = PdfAssets(MANIFEST)
    document = next(row for row in public_documents if row.provenance.method == method)
    pdf, pdf_metadata = assets.preview(document, variant=variant)
    encoded, metadata = assets.image(document, variant=variant)
    assert encoded.startswith(b"\x89PNG\r\n\x1a\n") and len(encoded) <= 15_000_000
    assert metadata["image_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert metadata["preview_sha256"] == pdf_metadata["preview_sha256"]
    assert metadata["original_sha256"] == document.provenance.source_sha256
    assert metadata["artifact_sha256"] == document.provenance.artifact_sha256
    assert metadata["selected_physical_page"] == physical and metadata["page_count"] == 1
    assert metadata["render_dpi"] == 108
    page = PdfReader(BytesIO(pdf)).pages[0]
    expected = (math.ceil(float(page.mediabox.width) * 1.5), math.ceil(float(page.mediabox.height) * 1.5))
    with Image.open(BytesIO(encoded)) as image:
        assert image.size == expected == (metadata["width_px"], metadata["height_px"])
        assert image.width <= 8000 and image.height <= 8000 and image.width * image.height <= 15_000_000
        rgb = image.convert("RGB")
        white = Image.new("RGB", image.size, "white")
        difference = ImageChops.difference(rgb, white)
        try:
            assert difference.getbbox() is not None  # The actual page renders visibly.
        finally:
            difference.close(); white.close(); rgb.close()


def test_native_and_raster_pages_render_different_actual_images(public_documents):
    assets = PdfAssets(MANIFEST)
    native = next(row for row in public_documents if row.provenance.method == "native")
    raster = next(row for row in public_documents if row.provenance.method == "ocr")
    assert assets.image(native)[1]["image_sha256"] != assets.image(raster)[1]["image_sha256"]


def test_png_render_serializes_pdfium_and_closes_all_documents(public_documents, monkeypatch):
    import pypdfium2 as pdfium
    original = pdfium.PdfDocument
    counter = {"active": 0, "peak": 0, "closed": 0}
    guard = threading.Lock()
    class TrackedDocument:
        def __init__(self, data):
            with guard:
                counter["active"] += 1
                counter["peak"] = max(counter["peak"], counter["active"])
            self.inner = original(data)
        def __getitem__(self, index):
            return self.inner[index]
        def close(self):
            self.inner.close()
            with guard:
                counter["active"] -= 1
                counter["closed"] += 1
    monkeypatch.setattr(pdfium, "PdfDocument", TrackedDocument)
    assets = PdfAssets(MANIFEST)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: assets.image(public_documents[0]), range(4)))
    assert len({metadata["image_sha256"] for _data, metadata in results}) == 1
    assert counter == {"active": 0, "peak": 1, "closed": 4}


def test_png_refuses_changed_snapshot_before_rendering(public_documents, tmp_path):
    assets = PdfAssets(MANIFEST)
    document = public_documents[0]
    key = (document.tenant_id, document.document_id)
    entry, mapping, source, artifact = assets.entries[key]
    changed = tmp_path / "changed-for-png.pdf"
    changed.write_bytes(source.read_bytes() + b"changed")
    assets.entries[key] = (entry, mapping, changed, artifact)
    with pytest.raises(ValueError, match="snapshot changed"):
        assets.image(document)


@pytest.mark.parametrize("dimensions", [
    (float("nan"), 100), (float("inf"), 100), (-1, 100),
    (5000.1 / 1.5, 2999.9 / 1.5),  # Float product fits; rounded bitmap does not.
])
def test_png_checks_exact_finite_pixel_allocation_before_render(public_documents, monkeypatch, dimensions):
    import pypdfium2 as pdfium
    closed = []
    class Page:
        def get_size(self): return dimensions
        def render(self, **kwargs): raise AssertionError("unbounded renderer must not run")
        def close(self): closed.append("page")
    class Document:
        def __init__(self, data): pass
        def __getitem__(self, index): return Page()
        def close(self): closed.append("pdf")
    monkeypatch.setattr(pdfium, "PdfDocument", Document)
    assets = PdfAssets(MANIFEST)
    monkeypatch.setattr(assets, "preview", lambda *args, **kwargs: (b"controlled verified one-page bytes", {}))
    with pytest.raises(ValueError, match="preview render"):
        assets.image(public_documents[0])
    assert closed == ["page", "pdf"]


@pytest.mark.parametrize("close_failure", [False, True])
def test_png_closes_every_native_handle_on_image_failure(public_documents, monkeypatch, close_failure):
    import pypdfium2 as pdfium
    closed = []
    class Image:
        size = (1, 1)  # Deliberately differs from its approved 150x150 allocation.
        def close(self):
            closed.append("image")
            if close_failure:
                raise RuntimeError("controlled image close failure")
    class Bitmap:
        def to_pil(self): return Image()
        def close(self): closed.append("bitmap")
    class Page:
        def get_size(self): return 100, 100
        def render(self, **kwargs): return Bitmap()
        def close(self): closed.append("page")
    class Document:
        def __init__(self, data): pass
        def __getitem__(self, index): return Page()
        def close(self): closed.append("pdf")
    monkeypatch.setattr(pdfium, "PdfDocument", Document)
    assets = PdfAssets(MANIFEST)
    monkeypatch.setattr(assets, "preview", lambda *args, **kwargs: (b"controlled verified one-page bytes", {}))
    with pytest.raises(RuntimeError if close_failure else ValueError):
        assets.image(public_documents[0])
    assert closed == ["image", "bitmap", "page", "pdf"]
