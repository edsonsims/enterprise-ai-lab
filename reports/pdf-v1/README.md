# Public PDF / OCR run

The recorded [demo.json](demo.json) comes from real local PDF parsing, PDFium
rendering, Tesseract, MiniLM ONNX, persistent Qdrant, Node OAuth issuance and
Python HTTP requests. The answer step is `extractive-v1`; no language model was
called. Runtime versions, English OCR data hash, pinned embedding revision and
complete canonical page texts are retained in the report.

| Observation | Recorded result |
| --- | --- |
| Native-text source, Alpha reader | Cites SP 800-207 physical page 15: access is granted per session |
| Focused OCR question, Alpha analyst | Cites artifact page 1 / AI 100-1 original page 25; retains GOVERN, MAP, MEASURE and MANAGE |
| Generic “What are the four functions?” | Abstains despite permitted OCR material; relevance remains a separate problem |
| Same OCR bytes, Alpha reader | Abstains with no citations under that tenant's analyst-only grant |
| Same OCR bytes, Beta reader | Returns OCR evidence under Beta's separate reader grant |
| Another tenant / missing access role | HTTP 403 |
| Re-ingest all four page grants | 28 chunks → 28 chunks; identical canonical texts and metadata |
| Restart Python without re-ingestion | Native and OCR answers retain source spans and citations |

This is **nine recorded cases over two distinct selected English pages**, not
an independent quality benchmark. The OCR input is a clean, explicitly labelled
lab-created raster derivative of a public document. Diagrams are not reconstructed;
line breaks, hyphenation and OCR spelling remain in the canonical text. Confidence
values are uncalibrated diagnostics.

The report is an **offline operator artifact containing every grant's full page
text**. It is not an authenticated API response and must not be used as a public
report template for private tenant documents. These particular documents are
public and retain [NIST source rights and attribution](../../public-data/pdf-v1/README.md).
API responses include only evidence selected from the caller's authorized context.

The first implementation split a native evidence sentence at the 500-character
boundary, causing an answerable question to abstain. PDF chunks now prefer complete
sentence boundaries within the same bound, without rewriting canonical text.
This fixes that boundary example; it does not establish general retrieval quality.
Controlled regression tests retain Unicode offsets and the original synthetic
study's chunking behavior.

The [native-layer/OCR diagnostic](text-layer-comparison.json) retains both full
streams and five token-alignment differences for the AI RMF page. OCR merges
`AI RMF` into `AIRMF` and adds diagram labels absent from the native extracted
layer. Punctuation spacing also differs. The native layer is a comparison stream,
not a human transcription; its alignment ratio is **not OCR accuracy**. Reproduce
with `python scripts/compare_pdf_text_layers.py` after installing both lockfiles.

Reproduce using the [PDF ingestion guide](../../docs/pdf-ingestion.md). Results
can differ across Tesseract/language-data versions; byte-identical OCR output is
only checked for repeated runs in the same recorded runtime. Owner human review
and study have not been reported.
