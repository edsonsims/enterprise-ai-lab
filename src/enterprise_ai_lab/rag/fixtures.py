"""Only authored synthetic notes are packaged; no customer documents are included."""
from pathlib import Path
from enterprise_ai_lab.core import read_jsonl
from .contracts import Document


def fixture_path() -> Path:
    return Path(__file__).with_name("data") / "documents.jsonl"


def load_documents(path=None) -> list[Document]:
    documents = []
    rows = read_jsonl(path or fixture_path())
    if not rows or len(rows) > 1000:
        raise ValueError("fixture must contain 1 to 1000 documents")
    for row in rows:
        if set(row) != {"document_id", "tenant_id", "allowed_roles", "title", "text"}:
            raise ValueError("invalid document fixture fields")
        if not isinstance(row["allowed_roles"], list):
            raise ValueError("allowed_roles must be an array")
        documents.append(Document(**{**row, "allowed_roles": tuple(row["allowed_roles"])}))
    if len({document.document_id for document in documents}) != len(documents):
        raise ValueError("duplicate document IDs")
    return documents
