"""Identity values here must come from a verified access token, never a request body."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Mapping, Protocol, Sequence

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


class QueryLimitError(ValueError):
    """A caller query exceeds a documented input boundary."""


class EmbeddingLimitError(ValueError):
    """An embedding text exceeds the pinned tokenizer's context window."""


def valid_identifier(value: object) -> bool:
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


@dataclass(frozen=True)
class RagPrincipal:
    tenant_id: str
    roles: tuple[str, ...]
    client_id: str

    def __post_init__(self):
        if not valid_identifier(self.tenant_id) or not valid_identifier(self.client_id):
            raise ValueError("invalid verified principal")
        if not isinstance(self.roles, tuple) or not self.roles or len(self.roles) > 16:
            raise ValueError("principal needs a nonempty role tuple")
        if any(not valid_identifier(role) for role in self.roles) or len(set(self.roles)) != len(self.roles):
            raise ValueError("invalid verified roles")


@dataclass(frozen=True)
class Document:
    document_id: str
    tenant_id: str
    allowed_roles: tuple[str, ...]
    title: str
    text: str

    def __post_init__(self):
        if not valid_identifier(self.document_id) or not valid_identifier(self.tenant_id):
            raise ValueError("invalid document identity")
        if not isinstance(self.allowed_roles, tuple) or not self.allowed_roles:
            raise ValueError("document ACL must allow at least one role")
        if (any(not valid_identifier(role) for role in self.allowed_roles)
                or len(set(self.allowed_roles)) != len(self.allowed_roles)):
            raise ValueError("invalid document ACL")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 200:
            raise ValueError("invalid document title")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 50_000:
            raise ValueError("invalid document text")
        try:
            self.text.encode("utf-8"); self.title.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("invalid document Unicode") from exc


@dataclass(frozen=True)
class RetrievedChunk:
    document_id: str
    chunk_id: str
    text: str
    score: float
    tenant_id: str
    allowed_roles: tuple[str, ...]

    def as_dict(self) -> dict:
        value = asdict(self)
        value["allowed_roles"] = list(self.allowed_roles)
        return value


class Generator(Protocol):
    def generate(self, question: str, chunks: Sequence[Mapping[str, object]]) -> dict: ...
