"""Authorized vector retrieval followed by an explicit grounded-answer baseline."""
from __future__ import annotations

import re
from .contracts import Generator, RagPrincipal

STOP_WORDS = frozenset("a an and are as at be by can do does for from how i in is it me of on or our the to up us we what when where which who why with your".split())


def _terms(text):
    return {word for word in re.findall(r"[a-z0-9]+", text.casefold()) if word not in STOP_WORDS}


class ExtractiveGenerator:
    """Copy one relevant sentence; no language model or semantic entailment claim.

    Deliberately conservative lexical coverage is an answerability guard on top of
    real dense-vector retrieval. Its threshold/word list are synthetic-demo choices.
    """
    name = "extractive-v1"

    def generate(self, question, chunks):
        terms = _terms(question)
        candidates = []
        for chunk in chunks:
            for sentence in re.split(r"(?<=[.!?])\s+", chunk["text"]):
                if terms and terms <= _terms(sentence):
                    candidates.append((len(sentence), chunk["chunk_id"], sentence))
        if not candidates:
            return {"answer": "I could not find an authorized passage that answers this question.",
                    "evidence": [], "generator": "extractive-v1"}
        _, chunk_id, quote = min(candidates)
        return {"answer": quote, "evidence": [{"chunk_id": chunk_id, "quote": quote}],
                "generator": "extractive-v1"}


class RagPipeline:
    def __init__(self, store, generator: Generator | None = None):
        self.store = store
        self.generator = generator or ExtractiveGenerator()

    def query(self, question: str, principal: RagPrincipal, top_k: int = 3) -> dict:
        # Keep policy mutation and query context/generation in one serial order.
        # A later local ACL update cannot race between retrieval and generation.
        with self.store.lock:
            chunks = self.store.search(question, principal, top_k)
            by_id = {chunk.chunk_id: chunk for chunk in chunks}
            if chunks:
                generated = self.generator.generate(question, [chunk.as_dict() for chunk in chunks])
            else:
                generated = {"answer": "I could not find an authorized passage that answers this question.",
                             "evidence": [], "generator": getattr(self.generator, "name", "unspecified")}
            if not isinstance(generated, dict) or not isinstance(generated.get("answer"), str):
                raise ValueError("invalid generator answer")
            if not isinstance(generated.get("generator"), str) or not generated["generator"]:
                raise ValueError("generator must identify its method")
            evidence = generated.get("evidence")
            if not isinstance(evidence, list) or len(evidence) > len(chunks):
                raise ValueError("invalid generator evidence")
            citations, seen = [], set()
            for item in evidence:
                if not isinstance(item, dict) or set(item) != {"chunk_id", "quote"}:
                    raise ValueError("invalid evidence fields")
                chunk_id, quote = item["chunk_id"], item["quote"]
                if (not isinstance(chunk_id, str) or chunk_id not in by_id or chunk_id in seen
                        or not isinstance(quote, str) or not quote.strip() or quote not in by_id[chunk_id].text):
                    raise ValueError("generator cited unavailable or changed context")
                seen.add(chunk_id)
                citations.append({**by_id[chunk_id].as_dict(), "quote": quote})
            # Never expose an unsupported model assertion when no evidence passed.
            answer = generated["answer"] if evidence else "I could not find an authorized passage that answers this question."
            response = {"answer": answer, "abstained": not bool(evidence), "citations": citations,
                        "evidence": evidence, "generator": generated["generator"],
                        "generation_performed": bool(chunks),
                        "identity": {"tenant_id": principal.tenant_id, "client_id": principal.client_id},
                        "human_review_required": True,
                        "limitations": "Exact quotations establish source support, not semantic entailment or advice."}
            if "metadata" in generated:
                response["metadata"] = generated["metadata"]
            if "generation" in generated:
                response["generation"] = generated["generation"]
            return response
