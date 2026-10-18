# Decision log

## 2026-10-17: one connected lab, evaluation first

**Context.** The goal is a balanced portfolio for senior software engineering and
applied AI/research engineering. The source list contains interdependent enterprise
components, many of which require identity systems, special hardware, real data, or
deployment environments.

**Decision.** Start with a dependency-light offline evaluation harness and a
permission-aware lexical retrieval fixture. Grow a connected lab rather than
declaring 15 complete projects. Keep core interfaces small enough to extract useful
standalone tools later.

**Reasoning.** The first experiment can run without credentials, customer data,
cloud spend, or special hardware. It establishes data contracts and negative tests
that later integrations must pass. The initial retrieval mechanism is deliberately
simple so permission and measurement behavior can be inspected directly.

**Alternatives.** Starting with a full identity deployment gives a stronger live
integration but delays measurement. Starting with 15 repositories makes scope easy
to browse but creates maintenance overhead before the components exist.

**Limits.** Lexical retrieval and fixture identities do not establish LLM answer
quality or production authorization. The synthetic cases are designed tests, not
an independent sample of customer traffic. AI-assisted implementation and automated
review are disclosed separately from the owner's personal study.


## 2026-10-17: connect identity before retrieved context

**Question.** Can an actual OAuth identity reach a vector-backed answer resource
without turning caller-supplied tenant/role fields into authorization?

**Decision.** Keep Python as the resource server. Verify the original issuer JWT
against an explicit loopback discovery/JWKS snapshot and a dedicated RAG audience.
Derive Qdrant's mandatory tenant AND role filter from the verified principal,
before top-k. Revalidate returned payloads before giving context to a generator.
The Node issuer and Python resource run as separate actual HTTP processes.

**Storage/model choice.** Qdrant local mode gives a persistent real vector backend
without Docker/cloud setup. MiniLM ONNX gives actual CPU dense embeddings, with
explicit download and hash-pinned offline assets. Local mode exercises embedded
storage and filtering, not a remote/distributed Qdrant deployment or HNSW scale.

**Answer choice.** An extractive reference makes the boundary easy to inspect.
The optional official Qwen3-0.6B adapter demonstrates actual CPU decoding with
strict JSON/evidence checks. Format failures and semantically weak answers remain
in the report; no silent fallback turns them into apparent neural success.
Exact quotation checks do not establish entailment or answer quality.

**Failure finding.** Qdrant's local persistence commits points individually.
An interrupted ACL/reindex operation can leave a partial state. All local
mutations therefore write a pending marker before changing points; a failed or
interrupted update blocks current queries and subsequent reopen. Recovery rebuilds
a fresh index from trusted inputs. Tests inject actual partially committed writes.
This favors denial over stale permissions and makes no power-loss guarantee.

**Evidence/limits.** The connected fixture has six authored documents/eight
chunks. Tests use actual HTTP, signatures, embeddings and persisted vectors;
negative cases also use controlled signed fixtures and fault injection. Offline
JWTs retain roles until expiry and use a startup key snapshot. Corpus permissions
are trusted authored metadata; live permission synchronization and production
identity lifecycle remain outside this milestone. Owner study is not claimed.
