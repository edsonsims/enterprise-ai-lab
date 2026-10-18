"""Exact spans in extracted page text; public source metadata is never policy."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WordBox:
    text: str
    start: int
    end: int
    left: int
    top: int
    width: int
    height: int
    confidence: float


@dataclass(frozen=True)
class SourceSpan:
    source_id: str
    source_uri: str
    source_sha256: str
    source_page: int
    artifact_sha256: str
    artifact_page: int
    method: str
    page_text_sha256: str
    start: int
    end: int
    ocr_words: tuple[WordBox, ...] = ()
    render_dpi: int | None = None
    width_px: int | None = None
    height_px: int | None = None

    def __post_init__(self):
        if not isinstance(self.source_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", self.source_id):
            raise ValueError("invalid provenance source ID")
        if not isinstance(self.source_uri, str) or len(self.source_uri) > 2048:
            raise ValueError("invalid provenance URI")
        uri = urlsplit(self.source_uri)
        try:
            self.source_uri.encode("utf-8")
            uri.port
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid provenance URI") from exc
        if (uri.scheme not in ("https", "http") or not uri.hostname or uri.username is not None
                or uri.password is not None or any(ord(c) <= 32 for c in self.source_uri)):
            raise ValueError("invalid provenance URI")
        for digest in (self.source_sha256, self.artifact_sha256, self.page_text_sha256):
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid provenance hash")
        if any(type(v) is not int or v < 1 for v in (self.source_page, self.artifact_page)):
            raise ValueError("invalid provenance page")
        if self.source_sha256 == self.artifact_sha256 and self.source_page != self.artifact_page:
            raise ValueError("identical PDF bytes must preserve original page numbers")
        if (type(self.start) is not int or type(self.end) is not int
                or not 0 <= self.start < self.end <= 80_000):
            raise ValueError("invalid provenance text span")
        if self.method not in ("native", "ocr") or not isinstance(self.ocr_words, tuple):
            raise ValueError("invalid extraction provenance")
        if self.method == "native":
            if self.ocr_words or any(v is not None for v in (self.render_dpi, self.width_px, self.height_px)):
                raise ValueError("native text cannot claim OCR coordinates")
        else:
            if (type(self.render_dpi) is not int or not 72 <= self.render_dpi <= 300
                    or any(type(v) is not int or v < 1 for v in (self.width_px, self.height_px))
                    or self.width_px * self.height_px > 15_000_000 or len(self.ocr_words) > 15_000):
                raise ValueError("invalid OCR page dimensions")
            previous = self.start
            for word in self.ocr_words:
                if (not isinstance(word, WordBox) or not isinstance(word.text, str) or not word.text
                        or any(type(v) is not int for v in (word.start, word.end, word.left, word.top, word.width, word.height))
                        or not self.start <= previous <= word.start < word.end <= self.end
                        or word.end - word.start != len(word.text)
                        or word.left < 0 or word.top < 0 or word.width <= 0 or word.height <= 0
                        or word.left + word.width > self.width_px or word.top + word.height > self.height_px
                        or type(word.confidence) not in (int, float) or not math.isfinite(word.confidence)
                        or not 0 <= word.confidence <= 100):
                    raise ValueError("invalid OCR word provenance")
                previous = word.end

    def validate_text(self, text: str):
        if self.end - self.start != len(text):
            raise ValueError("provenance length differs from exact text")
        for word in self.ocr_words:
            if text[word.start - self.start:word.end - self.start] != word.text:
                raise ValueError("OCR word offsets differ from exact text")

    def slice(self, start: int, end: int) -> SourceSpan:
        if not self.start <= start < end <= self.end:
            raise ValueError("chunk is outside source span")
        return replace(self, start=start, end=end,
            ocr_words=tuple(w for w in self.ocr_words if start <= w.start and w.end <= end))

    def as_dict(self) -> dict:
        value = asdict(self)
        value["ocr_words"] = list(value["ocr_words"])
        return value

    @classmethod
    def from_dict(cls, value: dict) -> SourceSpan:
        if not isinstance(value, dict) or set(value) != set(cls.__dataclass_fields__):
            raise ValueError("invalid provenance schema")
        value = dict(value)
        if not isinstance(value["ocr_words"], list):
            raise ValueError("invalid OCR word list")
        value["ocr_words"] = tuple(WordBox(**word) for word in value["ocr_words"])
        return cls(**value)
