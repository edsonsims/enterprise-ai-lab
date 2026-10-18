# Small corpus, deliberate conflicts

Every note in `documents.jsonl` was authored for this lab with AI assistance.
Alpha and Beta are fictional organizations. Limits, reserve percentages, internal
markers, and recovery procedures are invented. No customer, employer, financial,
or legal record was imported. The fixture is distributed under this repository's
MIT license.

The six documents produce eight chunks. Both tenants answer the same questions
with different numbers. Both have `reader` / `analyst` processing notes, while
reserve notes allow `analyst` only. This makes another tenant or role a plausible
nearest semantic neighbor, rather than relying on unrelated distractor text.
The Neptune password question has no supported answer. This is an integration
fixture, not a representative evaluation dataset or privacy certification.

The demo uses actual `sentence-transformers/all-MiniLM-L6-v2` embeddings, exported
to ONNX by Qdrant. The pinned upstream model revision, seven file hashes, original
model card, Apache-2.0 license declaration, and primary-source links are recorded
in `../model-lock.json`. Weights are downloaded explicitly into a local directory
and are not committed to this repository. Model verification and serving never
download files. The model uses 384 dimensions, CPU ONNX Runtime, and a 256-token
window; inputs beyond the window are rejected rather than silently truncated.

Qdrant local mode persists vectors and payloads on disk. Mandatory `tenant_id`
equality AND `allowed_roles` intersection filters run inside vector search before
the top-k limit. Returned payloads are checked again before context construction.
The default answer copies a matching sentence with an exact quote and chunk ID;
it is an extractive baseline, not language-model generation. Its lexical coverage
guard and cosine threshold 0.35 are transparent demo settings, not calibrated
confidence estimates or general answerability guarantees.

The local index directory is trusted administrative state. Qdrant local writes
persist per point, so a durable pending-write marker rejects all subsequent reads
and startup after an interrupted document/ACL mutation. Recovery is a rebuild into
a fresh directory, not deletion of the marker. This favors unavailability over a
partially applied permission update; it does not make local mode a production
database or protect files from a machine administrator.
