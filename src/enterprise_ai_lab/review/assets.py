"""Trusted PDF mappings; previews contain only the individually allowed page."""
from contextlib import ExitStack
from io import BytesIO
import hashlib
import json
import math
from pathlib import Path
import threading

_PDFIUM = threading.Lock()  # PDFium calls are serialized within this process.

from ..rag.pdf_corpus import FIELDS, _local_file, _read_regular, _unique


class PdfAssets:
    def __init__(self, manifest_path):
        path = Path(manifest_path).resolve(strict=True)
        manifest = json.loads(_read_regular(path, 512_000), object_pairs_hook=_unique)
        if (not isinstance(manifest, dict) or set(manifest) != {"schema_version", "entries"}
                or type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or not isinstance(manifest["entries"], list) or not 1 <= len(manifest["entries"]) <= 32):
            raise ValueError("invalid trusted preview manifest")
        self.entries = {}
        for entry in manifest["entries"]:
            if not isinstance(entry, dict) or set(entry) != FIELDS:
                raise ValueError("invalid preview grant")
            source = _local_file(path.parent, entry["source_path"], entry["source_sha256"])
            artifact = _local_file(path.parent, entry["artifact_path"], entry["artifact_sha256"])
            for mapping in entry["pages"]:
                identity = [entry["tenant_id"], entry["source_id"], mapping["source_page"]]
                document_id = "pdf-" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:40]
                key = (entry["tenant_id"], document_id)
                if key in self.entries:
                    raise ValueError("duplicate preview grant")
                self.entries[key] = (entry, mapping, source, artifact)

    def preview(self, document, *, variant="artifact"):
        from pypdf import PdfReader, PdfWriter
        if variant not in ("artifact", "original") or document.provenance is None:
            raise ValueError("invalid authorized preview")
        entry, mapping, source, artifact = self.entries[(document.tenant_id, document.document_id)]
        span = document.provenance
        if (span.source_id != entry["source_id"] or span.source_uri != entry["source_uri"]
                or span.source_sha256 != entry["source_sha256"] or span.artifact_sha256 != entry["artifact_sha256"]
                or span.source_page != mapping["source_page"] or span.artifact_page != mapping["artifact_page"]):
            raise ValueError("preview source differs from immutable candidate")
        # Verify both original and artifact snapshots on every read. Never trust
        # a browser path, filename or PDF metadata to select a document or ACL.
        original_bytes, artifact_bytes = _read_regular(source, 15_000_000), _read_regular(artifact, 15_000_000)
        if (hashlib.sha256(original_bytes).hexdigest() != span.source_sha256
                or hashlib.sha256(artifact_bytes).hexdigest() != span.artifact_sha256):
            raise ValueError("preview source snapshot changed")
        raw, number = (original_bytes, span.source_page) if variant == "original" else (artifact_bytes, span.artifact_page)
        reader = PdfReader(BytesIO(raw), strict=True)
        if reader.is_encrypted or not 1 <= number <= len(reader.pages):
            raise ValueError("invalid authorized preview page")
        page = reader.pages[number - 1]
        for key in ("/Annots", "/AA", "/OpenAction"):
            if key in page:
                del page[key]
        writer = PdfWriter()
        writer.add_page(page)
        writer.add_metadata({"/Title": "Authorized page preview", "/Subject":
            f"Lab preview derivative; {variant} physical page {number}; original SHA256 {span.source_sha256}"})
        output = BytesIO(); writer.write(output); data = output.getvalue()
        if len(data) > 15_000_000:
            raise ValueError("preview exceeds byte limit")
        return data, {"preview_sha256": hashlib.sha256(data).hexdigest(), "page_count": 1,
            "variant": variant, "selected_physical_page": number, "original_sha256": span.source_sha256,
            "artifact_sha256": span.artifact_sha256}

    def image(self, document, *, variant="artifact"):
        """Render verified one-page PDF bytes without a browser PDF plug-in."""
        import pypdfium2 as pdfium
        data, metadata = self.preview(document, variant=variant)
        with _PDFIUM, ExitStack() as cleanup:
            pdf = pdfium.PdfDocument(data)
            cleanup.callback(pdf.close)
            page = pdf[0]
            cleanup.callback(page.close)
            width, height = page.get_size()
            scale = 1.5
            if (not all(math.isfinite(value) and value > 0 for value in (width, height))):
                raise ValueError("preview render has invalid page dimensions")
            # PDFium rounds each scaled dimension upward before allocation.
            # Check that exact integer product, rather than its smaller float.
            expected = (math.ceil(width * scale), math.ceil(height * scale))
            if max(expected) > 8000 or expected[0] * expected[1] > 15_000_000:
                raise ValueError("preview render exceeds pixel bounds")
            bitmap = page.render(scale=scale)
            cleanup.callback(bitmap.close)
            image = bitmap.to_pil()
            cleanup.callback(image.close)
            if image.size != expected:
                raise ValueError("preview rendered dimensions differ from the bounded allocation")
            buffer = BytesIO(); image.save(buffer, format="PNG"); encoded = buffer.getvalue()
            if len(encoded) > 15_000_000:
                raise ValueError("preview image exceeds byte bound")
            return encoded, {**metadata, "image_sha256": hashlib.sha256(encoded).hexdigest(),
                "width_px": image.width, "height_px": image.height, "render_dpi": 108}
