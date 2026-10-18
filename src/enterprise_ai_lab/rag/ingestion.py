"""Local, bounded PDF extraction. Identity and permissions never come from a PDF.

Native text is a reading-order approximation, not a layout reconstruction. OCR
word confidence is a Tesseract diagnostic on its 0--100 scale, not a calibrated
probability. Offsets always address the exact canonical page text returned here.
No network, model inference, document indexing, or authentication happens here.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import closing
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from typing import Literal
from urllib.parse import urlsplit


class ExtractionError(ValueError):
    """A complete extraction was rejected; no partial page tuple is returned."""

    def __init__(self, message: str, *, code: str = "invalid_pdf"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ExtractionLimits:
    max_file_bytes: int = 15_000_000
    max_pdf_pages: int = 250
    max_selected_pages: int = 12
    max_page_pixels: int = 15_000_000
    max_page_content_bytes: int = 8_000_000
    max_page_chars: int = 80_000
    max_total_chars: int = 300_000
    max_ocr_words: int = 15_000
    dpi: int = 180
    ocr_timeout_seconds: float = 30.0
    process_timeout_seconds: float = 120.0

    def __post_init__(self):
        integers = (
            "max_file_bytes", "max_pdf_pages", "max_selected_pages",
            "max_page_pixels", "max_page_content_bytes", "max_page_chars",
            "max_total_chars", "max_ocr_words", "dpi",
        )
        for key in integers:
            value = getattr(self, key)
            if type(value) is not int or value <= 0:
                raise ValueError(f"{key} must be a positive integer")
        if self.dpi < 72 or self.dpi > 300:
            raise ValueError("dpi must be between 72 and 300")
        for key in ("ocr_timeout_seconds", "process_timeout_seconds"):
            value = getattr(self, key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 < value <= 600):
                raise ValueError(f"{key} must be finite and between 0 and 600")


@dataclass(frozen=True)
class OCRWord:
    text: str
    start: int
    end: int
    left: int
    top: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True)
class PageExtraction:
    source_id: str
    source_uri: str
    source_sha256: str
    page: int
    method: Literal["native", "ocr"]
    text: str
    page_text_sha256: str
    ocr_words: tuple[OCRWord, ...] = ()
    render_dpi: int | None = None
    width_px: int | None = None
    height_px: int | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _canonical_text(text: str) -> str:
    # Do not silently normalize Unicode or collapse interior spaces. These are
    # code-point offsets into this text, not byte offsets into a PDF stream.
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if "\x00" in text:
        raise ExtractionError("extracted text contains NUL")
    try:
        text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ExtractionError("extracted text contains invalid Unicode") from exc
    return text


def _selection(pages: tuple[int, ...], limit: int) -> None:
    if (not isinstance(pages, tuple) or not pages or len(pages) > limit
            or any(type(page) is not int or page < 1 for page in pages)
            or len(set(pages)) != len(pages)):
        raise ExtractionError("pages must be a nonempty tuple of unique 1-based integers",
                              code="invalid_selection")


def _source_identity(source_id: str, source_uri: str) -> None:
    if not isinstance(source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", source_id):
        raise ExtractionError("invalid public source identifier", code="invalid_source")
    if not isinstance(source_uri, str) or len(source_uri) > 2048:
        raise ExtractionError("invalid public source URI", code="invalid_source")
    try:
        parsed = urlsplit(source_uri)
        parsed.port  # Validate malformed ports even though this URI is never fetched.
        source_uri.encode("utf-8")
    except (ValueError, UnicodeEncodeError) as exc:
        raise ExtractionError("invalid public source URI", code="invalid_source") from exc
    if (parsed.scheme not in ("http", "https") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(ord(char) <= 32 for char in source_uri)):
        raise ExtractionError("public source URI must be HTTP(S) without credentials",
                              code="invalid_source")


def _terminate_process_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            # Kill remaining OCR children even if a resource limit has already
            # terminated the worker that originally led this process group.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    process.wait()


def extract_pdf(
    path: str | Path,
    *,
    source_id: str,
    source_uri: str,
    pages: tuple[int, ...],
    ocr_pages: tuple[int, ...] = (),
    limits: ExtractionLimits | None = None,
) -> tuple[PageExtraction, ...]:
    """Extract all explicitly selected pages or raise ``ExtractionError``.

    Empty native pages automatically use OCR; ``ocr_pages`` additionally forces
    rendering/OCR for selected pages whose embedded text is not trusted. PDF
    metadata, scripts, links, attachments, and embedded permission claims are
    never read as configuration. ``source_uri`` is recorded, never fetched.
    A bounded byte snapshot prevents a changed source path from changing the
    bytes under an already recorded hash. A child process bounds parser/render
    time and is killed with any OCR children when the total deadline expires.
    This is a local ingestion tool, not a sandbox for hostile uploads.
    """
    limits = limits or ExtractionLimits()
    _source_identity(source_id, source_uri)
    _selection(pages, limits.max_selected_pages)
    if (not isinstance(ocr_pages, tuple)
            or any(type(page) is not int or page not in pages for page in ocr_pages)
            or len(set(ocr_pages)) != len(ocr_pages)):
        raise ExtractionError("ocr_pages must be a unique subset of selected pages",
                              code="invalid_selection")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        with os.fdopen(descriptor, "rb") as stream:
            details = os.fstat(stream.fileno())
            if not stat.S_ISREG(details.st_mode):
                raise ExtractionError("input must be a regular local PDF file", code="invalid_file")
            if details.st_size > limits.max_file_bytes:
                raise ExtractionError("PDF exceeds file byte limit", code="resource_limit")
            data = stream.read(limits.max_file_bytes + 1)
    except (OSError, TypeError) as exc:
        raise ExtractionError("cannot read local PDF", code="invalid_file") from exc
    if len(data) > limits.max_file_bytes:
        raise ExtractionError("PDF exceeds file byte limit", code="resource_limit")
    if not data.startswith(b"%PDF-"):
        raise ExtractionError("input is not a PDF")
    source_sha256 = hashlib.sha256(data).hexdigest()
    with tempfile.TemporaryDirectory(prefix="rag-pdf-") as temporary:
        directory = Path(temporary)
        snapshot = directory / "source.pdf"
        snapshot.write_bytes(data)
        request = {
            "path": str(snapshot), "source_id": source_id, "source_uri": source_uri,
            "source_sha256": source_sha256, "pages": pages, "ocr_pages": ocr_pages,
            "limits": asdict(limits), "directory": str(directory),
        }
        with (directory / "response.json").open("w+b") as output:
            process = subprocess.Popen(
                [sys.executable, "-m", "enterprise_ai_lab.rag.ingestion", "--worker"],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=os.name == "posix",
            )
            try:
                process.communicate(json.dumps(request).encode(), timeout=limits.process_timeout_seconds)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(process)
                raise ExtractionError("PDF extraction process timed out", code="timeout") from exc
            if process.returncode != 0:
                _terminate_process_group(process)
                raise ExtractionError("PDF extraction worker failed", code="worker_failure")
            output.seek(0)
            response_bytes = output.read(24_000_001)
    if len(response_bytes) > 24_000_000:
        raise ExtractionError("extraction response exceeds limit", code="resource_limit")
    try:
        response = json.loads(response_bytes)
        if "error" in response:
            raise ExtractionError(response["error"]["message"], code=response["error"]["code"])
        records = []
        for value in response["pages"]:
            value["ocr_words"] = tuple(OCRWord(**word) for word in value["ocr_words"])
            records.append(PageExtraction(**value))
        if tuple(record.page for record in records) != pages:
            raise ValueError("page mismatch")
        return tuple(records)
    except ExtractionError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        raise ExtractionError("invalid extraction worker response", code="worker_failure") from exc


def _parse_tsv(tsv: str, width: int, height: int, max_words: int) -> tuple[str, tuple[OCRWord, ...]]:
    lines = tsv.splitlines()
    expected = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
    if not lines or lines[0] != expected:
        raise ExtractionError("invalid OCR TSV header", code="ocr_failure")
    pieces: list[str] = []
    words: list[OCRWord] = []
    position = 0
    previous_line = None
    for line in lines[1:]:
        if not line:
            continue
        fields = line.split("\t", 11)
        if len(fields) != 12:
            raise ExtractionError("invalid OCR TSV row", code="ocr_failure")
        try:
            if int(fields[0]) != 5:
                continue
            word_text = _canonical_text(fields[11])
            if not word_text:
                continue
            current_line = tuple(int(value) for value in fields[1:5])
            left, top, box_width, box_height = (int(value) for value in fields[6:10])
            confidence = float(fields[10])
            if (not math.isfinite(confidence) or not 0 <= confidence <= 100
                    or min(left, top) < 0 or min(box_width, box_height) <= 0
                    or left + box_width > width or top + box_height > height
                    or "\n" in word_text or "\t" in word_text):
                raise ValueError("invalid word coordinates or confidence")
        except (ValueError, OverflowError) as exc:
            raise ExtractionError("invalid OCR word values", code="ocr_failure") from exc
        if len(words) >= max_words:
            raise ExtractionError("OCR word count exceeds limit", code="resource_limit")
        separator = "" if not words else (" " if current_line == previous_line else "\n")
        pieces.append(separator)
        position += len(separator)
        start = position
        pieces.append(word_text)
        position += len(word_text)
        words.append(OCRWord(word_text, start, position, left, top, box_width, box_height, confidence))
        previous_line = current_line
    return "".join(pieces), tuple(words)


def _ocr_page(document, index: int, directory: Path, limits: ExtractionLimits) -> tuple:
    executable = shutil.which("tesseract")
    if executable is None:
        raise ExtractionError("Tesseract is required for scanned or forced-OCR pages", code="missing_ocr")
    with closing(document.get_page(index)) as page:
        page_width, page_height = page.get_size()
        if (not math.isfinite(page_width) or not math.isfinite(page_height)
                or min(page_width, page_height) <= 0):
            raise ExtractionError("invalid PDF page dimensions")
        scale = limits.dpi / 72
        width, height = math.ceil(page_width * scale), math.ceil(page_height * scale)
        if width * height > limits.max_page_pixels:
            raise ExtractionError("rendered page exceeds pixel limit", code="resource_limit")
        image_path = directory / f"page-{index + 1}.png"
        with closing(page.render(scale=scale)) as bitmap:
            image = bitmap.to_pil()
            try:
                width, height = image.size
                if width * height > limits.max_page_pixels:
                    raise ExtractionError("rendered page exceeds pixel limit", code="resource_limit")
                image.save(image_path, format="PNG")
            finally:
                image.close()
    output_base = directory / f"ocr-{index + 1}"
    try:
        completed = subprocess.run(
            [executable, str(image_path), str(output_base), "-l", "eng", "--psm", "3", "tsv"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=limits.ocr_timeout_seconds,
            env={**os.environ, "OMP_THREAD_LIMIT": "1"},
        )
    except subprocess.TimeoutExpired as exc:
        raise ExtractionError("OCR page timed out", code="timeout") from exc
    finally:
        image_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        raise ExtractionError("Tesseract OCR failed", code="ocr_failure")
    tsv_path = output_base.with_suffix(".tsv")
    try:
        with tsv_path.open("rb") as stream:
            tsv_bytes = stream.read(8_000_001)
    except OSError as exc:
        raise ExtractionError("OCR did not produce TSV", code="ocr_failure") from exc
    if len(tsv_bytes) > 8_000_000:
        raise ExtractionError("OCR output exceeds byte limit", code="resource_limit")
    try:
        text, words = _parse_tsv(tsv_bytes.decode("utf-8"), width, height, limits.max_ocr_words)
    except UnicodeDecodeError as exc:
        raise ExtractionError("invalid OCR output Unicode", code="ocr_failure") from exc
    return text, words, width, height


def _extract_worker(request: dict) -> list[dict]:
    from pypdf import PdfReader
    import pypdfium2

    limits = ExtractionLimits(**request["limits"])
    # Linux adds a parser/render address-space and CPU ceiling. macOS does not
    # implement a reliable RLIMIT_AS; the deadline and structural caps still
    # apply there. Neither mode claims an upload security sandbox.
    if sys.platform.startswith("linux"):
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (1_500_000_000, 1_500_000_000))
        cpu_seconds = math.ceil(limits.process_timeout_seconds) + 1
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    path = Path(request["path"])
    try:
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted:
            raise ExtractionError("encrypted PDFs are not accepted", code="encrypted_pdf")
        count = len(reader.pages)
        if count < 1:
            raise ExtractionError("PDF has no pages")
        if count > limits.max_pdf_pages:
            raise ExtractionError("PDF page count exceeds limit", code="resource_limit")
        if any(page > count for page in request["pages"]):
            raise ExtractionError("selected page is outside the PDF", code="invalid_selection")
        result = []
        total_chars = 0
        rendered_document = None
        try:
            for number in request["pages"]:
                page = reader.pages[number - 1]
                method = "native"
                words = ()
                width = height = dpi = None
                if number in request["ocr_pages"]:
                    text = ""
                else:
                    contents = page.get_contents()
                    if contents is not None and len(contents.get_data()) > limits.max_page_content_bytes:
                        raise ExtractionError("decoded page content exceeds limit", code="resource_limit")
                    text = _canonical_text(page.extract_text(extraction_mode="plain") or "")
                if not text:
                    if rendered_document is None:
                        rendered_document = pypdfium2.PdfDocument(path)
                        if len(rendered_document) != count:
                            raise ExtractionError("PDF parsers disagree about page count")
                    text, words, width, height = _ocr_page(
                        rendered_document, number - 1, Path(request["directory"]), limits)
                    method = "ocr"
                    dpi = limits.dpi
                if not text.strip():
                    raise ExtractionError(f"selected page {number} has no extractable text", code="empty_page")
                if len(text) > limits.max_page_chars:
                    raise ExtractionError("page text exceeds character limit", code="resource_limit")
                total_chars += len(text)
                if total_chars > limits.max_total_chars:
                    raise ExtractionError("selected text exceeds total character limit", code="resource_limit")
                result.append(PageExtraction(
                    request["source_id"], request["source_uri"], request["source_sha256"],
                    number, method, text, hashlib.sha256(text.encode()).hexdigest(), words,
                    dpi, width, height,
                ).as_dict())
            return result
        finally:
            if rendered_document is not None:
                rendered_document.close()
            reader.close()
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError("PDF parser or renderer rejected the document") from exc


def _main_worker() -> None:
    try:
        request = json.load(sys.stdin)
        value = {"pages": _extract_worker(request)}
    except ExtractionError as exc:
        value = {"error": {"code": exc.code, "message": str(exc)}}
    except Exception:
        value = {"error": {"code": "worker_failure", "message": "PDF extraction worker failed"}}
    print(json.dumps(value, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    if sys.argv[1:] != ["--worker"]:
        raise SystemExit("This module is an internal extraction worker; use the lab ingestion CLI.")
    _main_worker()
