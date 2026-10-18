"""Diagnostic text-layer/OCR comparison; neither stream is human ground truth."""
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import re

from enterprise_ai_lab.rag.ingestion import extract_pdf
from enterprise_ai_lab.rag.pdf_demo import extraction_runtime


def main():
    root = Path(__file__).resolve().parents[1]
    data = root / "public-data/pdf-v1"
    manifest = json.loads((data / "manifest.json").read_text())
    artifacts = {row["artifact_id"]: row for row in manifest["artifacts"]}
    native = artifacts["nist-ai-100-1-original"]
    raster = artifacts["nist-ai-100-1-page25-raster"]
    for item in (native, raster):
        if hashlib.sha256((data / item["path"]).read_bytes()).hexdigest() != item["sha256"]:
            raise ValueError("public PDF differs from pinned source")
    reference = extract_pdf(data / native["path"], source_id="nist-ai-rmf",
        source_uri=native["source_url"], pages=(25,))[0]
    ocr = extract_pdf(data / raster["path"], source_id="nist-ai-rmf",
        source_uri=native["source_url"], pages=(1,))[0]
    # Keep punctuation/case in the retained complete streams. The diagnostic
    # below only separates non-whitespace tokens and lowercases them.
    def tokens(text):
        return [word.casefold() for word in re.findall(r"\S+", text)]
    before, after = tokens(reference.text), tokens(ocr.text)
    alignment = SequenceMatcher(None, before, after, autojunk=False)
    differences = [{"operation": tag, "native_token_span": [a,b], "ocr_token_span": [c,d],
        "native_tokens": before[a:b], "ocr_tokens": after[c:d]}
        for tag,a,b,c,d in alignment.get_opcodes() if tag != "equal"]
    result = {"schema_version": 1, "runtime": extraction_runtime(),
        "comparison": "Native PDF text layer versus actual OCR of a clean raster derivative",
        "ground_truth": False, "human_transcription_performed": False,
        "source_sha256": native["sha256"], "source_page": 25,
        "artifact_sha256": raster["sha256"], "artifact_page": 1,
        "native": reference.as_dict(), "ocr": ocr.as_dict(),
        "diagnostics": {"native_tokens": len(before), "ocr_tokens": len(after),
            "sequence_matcher_ratio": alignment.ratio(), "differences": differences},
        "limitations": ["Alignment ratio is not OCR accuracy; the native layer also loses layout.",
            "Case-folded whitespace tokens preserve hyphenation and punctuation differences.",
            "A single clean English page does not represent scanned-document quality."]}
    output = root / "reports/pdf-v1/text-layer-comparison.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"output": "reports/pdf-v1/text-layer-comparison.json", **result["diagnostics"], "differences": len(differences)}))


if __name__ == "__main__":
    main()
