#!/usr/bin/env python3
"""Rebuild the image-only OCR fixture from the byte-pinned public original.

This is fixture authoring, not ingestion. It requires Poppler's pdftoppm plus
ReportLab, Pillow and pypdf. Rebuilds can differ across renderer/library versions;
the checked-in artifact and manifest hashes identify the actual demo input.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from PIL import Image
from pypdf import PdfReader
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'public-data/pdf-v1/nist-ai-100-1.pdf'
SOURCE_SHA256 = '7576edb531d9848825814ee88e28b1795d3a84b435b4b797d3670eafdc4a89f1'
BUILD_DATE = '2026-10-18T02:25:46+00:00'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True,
                        help='New output path; existing files are never replaced.')
    parser.add_argument('--pdftoppm', default=shutil.which('pdftoppm'))
    args = parser.parse_args()
    if not args.pdftoppm:
        parser.error('Install Poppler or supply --pdftoppm /path/to/pdftoppm.')
    if hashlib.sha256(SOURCE.read_bytes()).hexdigest() != SOURCE_SHA256:
        parser.error('The original PDF does not match its pinned SHA-256.')
    if args.output.exists():
        parser.error('The output already exists; choose a new path.')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='public-pdf-fixture-') as temporary:
        prefix = Path(temporary) / 'page'
        subprocess.run([args.pdftoppm, '-f', '25', '-l', '25', '-r', '200',
                        '-png', '-singlefile', str(SOURCE), str(prefix)],
                       check=True, timeout=60, stdout=subprocess.DEVNULL)
        with Image.open(prefix.with_suffix('.png')) as image:
            if image.size != (1700, 2200):
                raise ValueError('The source page did not render to the expected letter size.')
            raster = image.convert('RGB')
        previous_epoch = os.environ.get('SOURCE_DATE_EPOCH')
        os.environ['SOURCE_DATE_EPOCH'] = str(int(datetime.fromisoformat(BUILD_DATE).timestamp()))
        try:
            output = canvas.Canvas(str(args.output), pagesize=(612, 792),
                                   invariant=True, pageCompression=1)
            output.setTitle('Rasterized OCR fixture: NIST AI 100-1, original PDF page 25')
            output.setAuthor('Enterprise AI Lab; source: National Institute of Standards and Technology')
            output.setSubject('Image-only derivative created 2026-10-18 for OCR testing; not a historical scan.')
            output.drawImage(ImageReader(raster), 0, 0, width=612, height=792)
            output.showPage()
            output.save()
        finally:
            if previous_epoch is None:
                os.environ.pop('SOURCE_DATE_EPOCH', None)
            else:
                os.environ['SOURCE_DATE_EPOCH'] = previous_epoch
    result = PdfReader(args.output)
    if len(result.pages) != 1 or result.pages[0].extract_text().strip():
        args.output.unlink()
        raise ValueError('The OCR fixture must contain one page and no PDF text layer.')
    data = args.output.read_bytes()
    print(f'{hashlib.sha256(data).hexdigest()}  {args.output} ({len(data)} bytes)')


if __name__ == '__main__':
    main()
