# Public PDFs, traceable pages

Two NIST publications give the demo real source material while keeping the
experiment small. The original downloads are preserved byte-for-byte. The demo
selects one page from each; it does not ingest the whole publications.

| Input | Page used | Extraction | What to ask |
| --- | --- | --- | --- |
| [Zero Trust Architecture](nist-sp-800-207.pdf) | Physical PDF page 15, printed page 6 | Native PDF text | How is access to individual enterprise resources granted? |
| [AI RMF Core raster fixture](nist-ai-100-1-page25-raster.pdf) | Fixture page 1, mapped to original PDF page 25 / printed page 20 | Tesseract OCR | What functions is the Core composed of? |

The second input is **a lab-created image-only derivative**, not a historical
scan. It renders one page of the [unchanged AI RMF original](nist-ai-100-1.pdf) at
200 dpi and embeds that image in a new PDF without a text layer. This exercises
real rendering and OCR. Its clean page does not establish accuracy on skewed,
damaged, handwritten or multilingual scans. The original page includes a diagram;
OCR reading order is not a faithful reconstruction of that layout.

`manifest.json` records the source URLs, full SHA-256 hashes, byte counts, page
mapping, modification notice and tool versions. Physical PDF page numbers are
one-based; printed labels differ because the publications have front matter.
Citation provenance must retain both the submitted artifact hash/page and the
original publication hash/page. A public URL is a citation, never permission to
fetch arbitrary content during ingestion.

The documents are public; Alpha and Beta tenant assignments and role grants are
authored test policy. Access to the public website does not bypass the local
resource's permission checks. These examples are integration fixtures, not a
privacy certification, compliance assessment or general retrieval benchmark.

## Rights and attribution

These source documents keep their own rights. The repository's MIT license
applies to the lab code and does not relicense the publications.

The [NIST Technical Series policy](https://www.nist.gov/open/copyright-fair-use-and-licensing-statements-srd-data-software-and-technical-series-publications)
describes U.S. federal-work status and grants worldwide republication and
derivative rights to the extent NIST may assert rights outside the United
States. Third-party material can carry separate rights. SP 800-207 also contains
an explicit U.S. copyright statement. No separately marked third-party material
was identified on the selected pages. Source rights and links were checked on
October 18, 2026 UTC, which was October 17 in Toronto.

- Rose, S., Borchert, O., Mitchell, S., and Connelly, S. (2020), *Zero Trust
  Architecture*, NIST SP 800-207, National Institute of Standards and Technology,
  [doi:10.6028/NIST.SP.800-207](https://doi.org/10.6028/NIST.SP.800-207).
- National Institute of Standards and Technology (2023), *Artificial Intelligence
  Risk Management Framework (AI RMF 1.0)*, NIST AI 100-1,
  [doi:10.6028/NIST.AI.100-1](https://doi.org/10.6028/NIST.AI.100-1).

Republished courtesy of the National Institute of Standards and Technology.
The raster fixture was modified on October 18, 2026 UTC as described above.
NIST has not reviewed or endorsed this lab.

## Rebuild the image-only fixture

The checked-in file is the actual demo input. Rebuilding requires Poppler's
`pdftoppm` and Python packages `reportlab==4.4.9`, `Pillow==12.3.0` and
`pypdf==6.10.0`; these authoring tools are separate from the ingestion runtime.

From the repository root:

```sh
python scripts/build-public-pdf-fixture.py \
  --output .local/rebuilt-ai-rmf-page25.pdf
```

The helper verifies the original's hash, renders only page 25, refuses to replace
an existing output, and checks that the result has one page and no text layer.
It fixes PDF metadata to the first artifact build time for reproducibility.
The recorded Poppler version is 26.05.0; different renderer or library versions
can change bytes, so the manifest hash identifies the committed fixture rather
than promising identical output from every environment. Neither rebuilding nor
running the demo downloads documents.
