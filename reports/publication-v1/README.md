# Approved documents → authorized RAG context

This report records the connected local publication study. It reuses the public
NIST native/OCR pages and their authored tenant/role grants, adds immutable
content-bound review and publishes to the actual MiniLM/Qdrant RAG only after an
independent scripted OAuth identity approves.

The actual local run records **48 HTTP observations, three committed publication
effects and six exported/replayed Temporal histories**. The real Qdrant inventory
contains **zero points** after staging, rejection, expiry and a pending request;
it ends at **28 chunks** after the two tenant publications and Alpha's approved
ACL revision. Repeating an approval does not duplicate a publication.

The runtime uses Python 3.13.1/macOS arm64, Temporal Python SDK 1.34.0, pinned
CLI 1.9.1/server 1.32.0, MiniLM ONNX through ONNX Runtime 1.31.0, Qdrant client
1.19.1 and actual Tesseract 5.5.1 English OCR. `demo.json` retains the exact source,
model, executable and identity-bridge fingerprints and full HTTP responses.

The complete local suite passed **354 tests and 247 subtests** on Python
3.13.1/macOS arm64 with all actual integration flags enabled. This includes
65 publication tests: real process/model checks plus explicitly controlled
boundary, vector, fault and lock-admission cases. The original dependency-free
14-test suite also passed and reproduced its reference files without a diff.

Observed process boundaries:

| Exercise | Recorded result |
| --- | --- |
| Temporal server, publication resource/worker and approval edge restart while review is pending | Same immutable request; no searchable candidate |
| Resource/worker killed after the vectors and ledger commit, before activity acknowledgement | Temporal retries attempt 2 after a StartToClose timeout; one matching Alpha effect |
| Reader and analyst query the same approved OCR source | Alpha analyst receives cited OCR evidence; Alpha reader abstains |
| Identical public source under a separate Beta approval | Beta reader receives its own tenant's cited evidence |
| Alpha narrows its native page from reader to analyst | Old approved content remains live while pending; after approval reader access disappears and analyst access remains |
| Older accepted decision is replayed; a different stale revision is approved | Replay preserves current ACL; stale publication fails without an effect |
| Resource restarts after publication | OCR citations and narrowed ACL persist |
| Six exported histories run through SDK Replayer | All replay; no new publication rows |

The actual issuer remains running during these recovery probes so its signing
keys and startup JWKS remain valid. The process failure is injected after both
stores commit; it is separate from the controlled storage tests that exercise
the vector/ledger gap and partial vector writes. Partial writes retain the
original fail-closed marker and require a fresh index rebuild.

Reproduce using the complete setup in the guide:

```sh
.venv-publication/bin/python -m enterprise_ai_lab.publication demo \
  --temporal-cli .local/temporal-bin/temporal \
  --identity-dir ../identity-boundary-lab --node node \
  --model-dir .local/models/minilm \
  --manifest public-data/pdf-v1/ingestion.json \
  --work-dir .local/publication-demo \
  --out reports/local/publication-demo.json
```

Use a fresh work directory. Platform/OCR runtimes can produce different text or
timing observations, so a new run writes a separate report rather than replacing
the retained evidence. The report and raw histories exclude bearer tokens;
decoded history payloads are also checked. The workflow contains the candidate
hash, not the complete PDF text; full candidate and published text live in the
local SQLite repository.

The [reproduction guide](../../docs/approved-publication.md) explains the gate,
revision checks, pending-read fence and legacy standalone ingestion boundary.
Earlier reports under `rag-v1`, `pdf-v1` and `approval-v1` are preserved separately.

The reviewer is a client-credentials service fixture, not a logged-in person or
evidence of owner review/study. The answer step is extractive, with no new neural
model evaluation. Two selected public pages and one clean raster OCR derivative
do not establish broad OCR, security or answer-quality performance. The public
source documents retain their [rights and attribution](../../public-data/pdf-v1/README.md).
