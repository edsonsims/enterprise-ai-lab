"""Pinned CPU embeddings. Download is explicit; inference is always offline."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from threading import RLock
from .contracts import EmbeddingLimitError


def model_lock() -> dict:
    return json.loads(Path(__file__).with_name("model-lock.json").read_text(encoding="utf-8"))


def verify_model(model_dir) -> dict:
    directory = Path(model_dir)
    lock = model_lock()
    for name, expected in lock["files"].items():
        path = directory / name
        if not path.is_file() or path.stat().st_size != expected["size"]:
            raise ValueError(f"missing or changed embedding asset: {name}; run explicit model bootstrap")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected["sha256"]:
            raise ValueError(f"embedding asset hash mismatch: {name}")
    return {"repository": lock["repository"], "revision": lock["revision"],
            "dimensions": lock["dimensions"], "verified_files": len(lock["files"]),
            "model_sha256": lock["files"]["model.onnx"]["sha256"]}


def bootstrap_model(model_dir) -> dict:
    """The only embedding function that permits a network download."""
    from huggingface_hub import snapshot_download

    directory = Path(model_dir)
    lock = model_lock()
    if all((directory / name).is_file() for name in lock["files"]):
        return verify_model(directory)
    snapshot_download(repo_id=lock["repository"], revision=lock["revision"],
                      allow_patterns=list(lock["files"]), local_dir=str(directory), token=False)
    return verify_model(directory)


class LocalEmbedder:
    """Real 384-dimensional MiniLM ONNX inference, CPU only, 256-token limit."""
    dimensions = 384

    def __init__(self, model_dir):
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer

        self.manifest = verify_model(model_dir)
        self.fingerprint = self.manifest["model_sha256"]
        self._lock = RLock()
        self._tokenizer = Tokenizer.from_file(str(Path(model_dir) / "tokenizer.json"))
        self._tokenizer.no_truncation()
        self._model = TextEmbedding(model_name=model_lock()["model_name"],
                                    specific_model_path=str(Path(model_dir).resolve()),
                                    local_files_only=True, providers=["CPUExecutionProvider"],
                                    threads=1)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or not texts or len(texts) > 2000:
            raise ValueError("embed expects 1 to 2000 texts")
        with self._lock:
            for text in texts:
                if not isinstance(text, str) or not text.strip() or len(text) > 50_000:
                    raise ValueError("embedding text must be nonempty and bounded")
                try:
                    text.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise ValueError("invalid Unicode") from exc
                if len(self._tokenizer.encode(text).ids) > model_lock()["max_tokens"]:
                    raise EmbeddingLimitError("embedding input exceeds 256 tokens; shorten the query or chunk")
            vectors = [vector.tolist() for vector in self._model.embed(texts, batch_size=32)]
        if len(vectors) != len(texts) or any(len(vector) != self.dimensions for vector in vectors):
            raise RuntimeError("embedding model returned an unexpected shape")
        return vectors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("bootstrap", "verify"))
    parser.add_argument("--model-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    result = bootstrap_model(args.model_dir) if args.action == "bootstrap" else verify_model(args.model_dir)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
