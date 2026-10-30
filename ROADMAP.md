# Enterprise AI engineering study roadmap

The sequence balances applied AI evaluation with identity, data, reliability, and
operations. It adapts the 15 project prompts in [Suraj Sharma's October 17, 2026
post](https://x.com/suraj_sharma14/status/2111427008295301488). These are study
directions, not evidence of employability, enterprise adoption, or research novelty.

Scheduled work advances by at most one substantive milestone per day. A project may need
several sessions. A finished milestone needs runnable code, relevant tests, a clear
explanation, and evidence of what was actually tested. Integrations remain planned
until exercised against the real dependency. The three eventual case studies share
these components; separate repositories are useful only when a module stands alone.

| Order | Study (source item) | Required evidence before completion | Status |
| --- | --- | --- | --- |
| 1 | Customer data evaluation harness (10) | Strict dataset/prediction contract, baseline comparison, per-case results, reproducibility and limitations | Offline synthetic milestone implemented 2026-10-17; real-data validation pending |
| 2 | Technical discovery to SOW translator (11) | Fictional brief, explicit assumptions, acceptance criteria, scope and architecture; no invented client | [BriefTrace](https://github.com/edsonsims/brieftrace): source-linked compiler, open questions, proposed criteria, dependency layers and transactional SQLite revision API implemented 2026-10-17. Model extraction, architecture recommendation and commercial SOW approval remain unimplemented. |
| 3 | Enterprise SSO and RBAC bridge (1) | Local standards-based identity provider, token validation, revocation/role tests; SAML and SCIM tracked separately | [Identity Boundary Lab](https://github.com/edsonsims/identity-boundary-lab): real local OAuth client credentials, JWKS, audience/tenant/RBAC/scope checks and paired-token revocation experiment implemented 2026-10-17. A separate local browser issuer adds actual account login with Authorization Code + PKCE and role-bounded review tokens on 2026-10-18. External SSO, SAML, SCIM, persistence and key rotation remain planned. |
| 4 | Permission-filtered RAG engine (4) | Authenticated identity to query filter, vector backend, tenant/role isolation and retrieval-quality tests | Connected local milestone implemented 2026-10-17: actual OAuth issuer/JWKS, dedicated JWT audience/scope, MiniLM ONNX embeddings, persistent Qdrant local mode, tenant AND role prefilter, cited extractive answers and optional actual Qwen CPU generation. Real HTTP/restart/fault tests and retained generation failures. Public PDF/OCR sources extend the original synthetic corpus. The [approved-publication resource](docs/approved-publication.md) now gates its searchable corpus on an immutable durable approval, with revision conflicts and fail-closed vector/ledger recovery. The [browser review room](docs/human-review.md) adds account-scoped PDF/text inspection and durable decisions before the same publication path. Production IAM/permission synchronization, distributed vector serving and independent answer-quality evaluation remain planned. |
| 5 | Legacy ETL and OCR ingestor (3) | Licensed scanned fixtures, provenance, idempotency, OCR error analysis and permission propagation | [Local PDF/OCR milestone](docs/pdf-ingestion.md): real native extraction and Tesseract over a labelled raster derivative of a public NIST page; original/artifact hashes and pages, exact text spans, OCR word boxes, trusted tenant/role grants and stable tenant-scoped IDs. Actual OAuth/HTTP/vector/restart tests and batch/write-failure boundaries. The connected approval path stages this exact content/ACL/provenance outside search before publication; standalone trusted ingestion remains separate. The browser review extension serves individually permitted single-page previews, with original/artifact/preview hashes kept distinct and canonical chunk spans preserved. Two selected English pages, one clean OCR page; difficult-scan accuracy, broader OCR error analysis, legacy connectors and live permission synchronization remain planned. |
| 6 | Durable human approval gate (7) | Temporal restart/replay tests, expiry, idempotency, forged approval rejection; local notifier first | [Local approval milestone](docs/durable-approval.md) implemented 2026-10-18: actual persistent Temporal, validated immutable snapshots, independent service-identity reviewer, durable expiry, transactional local note sink, restart/worker-kill/replay evidence and authenticated HTTP boundaries. The [connected publication extension](docs/approved-publication.md) binds a staged RAG candidate and expected revision, publishes through a single vector owner, and preserves tenant/role/source evidence. The [browser review extension](docs/human-review.md) adds actual local Code + PKCE login, PDF/chunk inspection, complete-batch permission checks, independent account approval/rejection and hash-linked local audit with intent/receipt reconciliation. Its automated fixture actions do not establish owner or human review. Production gRPC authorization, external identity, retention/migration policy and external actions remain planned. |
| 7 | Legacy MCP gateway (5) | Official protocol client/server interoperability, least-privilege DB access, allowlisted operations and audit | [Local SQLite milestone](docs/sqlite-mcp-gateway.md) implemented 2026-10-19: actual official MCP2 stdio discovery/legacy handshake, fixed read-only tools, trusted launcher profiles, tenant AND row-role SQL prefilter, bounded queries, metadata-only durable audit and real process/lock/corruption/cancellation tests. Authored synthetic work orders; remote identity, Oracle/SAP, approved writes and production operations remain planned. |
| 8 | PII redaction and restoration proxy (6) | Synthetic test corpus, precision/recall, scoped token mapping, collision and log-leak tests | [Local scoped milestone](docs/privacy-proxy.md) implemented 2026-10-20: actual dedicated OAuth JWT/HTTP boundary, tenant AND client AND context restoration, private SQLite map, random-token collision/expiry/capacity/rollback tests and actual local processor/restart/log observations. Frozen 36-case development evaluation retains narrow detector errors and unsupported formats. Comprehensive detection, encrypted storage, secure erasure and external providers remain unimplemented. |
| 9 | Chaos and failover suite (13) | Injected timeouts, bounded retries, failure recovery and measured degradation | [Local read-only MCP milestone](docs/chaos-failover.md) implemented 2026-10-21: actual fixed stdio replicas, immutable fixture/profile checks, absolute work budgets and at most one transport-only failover. Real process kill/stall/disconnect, terminal permission/audit/payload cases, preserved incomplete intents, cancellation and explicit recovery; observed timings and reaped children. Remote topology, write retries and production availability remain planned. |
| 10 | Multi-tenant cost attribution (14) | Usage ledger, reconciliation, decimal arithmetic, tenant isolation and anomaly cases | [Local fictional milestone](docs/cost-attribution.md) implemented 2026-10-22: exact Decimal charges, tenant-local idempotent usage, immutable statement snapshots and independently authored anomalies; actual SQLite/CLI restart, concurrent duplicate and lost-receipt recovery. Trusted launcher profiles are not authentication. Provider telemetry/pricing, billing, credits/FX/tax and production operations remain planned. |
| 11 | Customer health and ROI dashboard (9) | Traceable usage/cost, explicit baseline and assumptions, uncertainty and sensitivity | [Local fictional activity and modeled ROI milestone](docs/activity-roi.md) implemented2026-10-23: one tenant-local ledger snapshot, exact request/charge references, declared activity flags and27 rational-arithmetic sensitivity scenarios, private offline HTML/JSON and actual CLI/restart/isolation/export tests. Real task outcomes, measured savings, causal evaluation, customer integrations and authenticated serving remain planned. |
| 12 | Air-gapped deployment pipeline (2) | Repeatable offline artifact bundle, image inventory and tested egress denial in an isolated environment | [Core wheel milestone](docs/offline-bundle.md) implemented2026-10-24: fixed-input repeatable inventory, independent expected digest, strict verifier and actual fresh installation/lexical demo in a macOS process with network denied. Image/model/dependency inventories are explicitly empty; full optional runtime/container delivery and physical air-gap validation remain planned. |
| 13 | On-prem edge inference node (8) | Actual supported local model, measured latency/memory and reproducible configuration | [CPU local-worker milestone](docs/edge-inference.md) implemented 2026-10-25: pinned Qwen3-0.6B, strict stdio requests, actual first/warm timings and tensor/call counts, whole-process peak RSS and 1/4 intraop-thread comparison from a locally installed wheel under macOS process network denial. Fictional inputs and retained failures; no quality, hardware-superiority or OS-cold-cache claim. NVIDIA vLLM/TensorRT/MIG, fleet operations, authenticated network serving and optional-runtime delivery remain planned. |
| 14 | Self-service admin and runbook portal (12) | Authenticated admin boundary, safe config updates, audit and recovery exercise | [Local configuration desk milestone](docs/admin-portal.md) implemented2026-10-26: dedicated real Code+PKCE user identity, two allowlisted fictional view settings, tenant AND row-role source filtering, atomic SQLite revisions/audit/receipts, exact retry and append-only rollback. Actual browser and before/after-commit process interruption exercises; production operations, external SSO, MFA and live IAM remain planned. |
| 15 | Three public deployment case studies (15) | Reproduce and explain the trust boundary, reliability, and measurement workflows below | In progress: [historical evidence inventory](docs/case-studies/index.md) implemented 2026-10-28; the [first fresh permission-aware workflow](reports/case-knowledge-v1) ran 2026-10-29 from an installed wheel with actual OAuth/OCR/Temporal/MiniLM/Qdrant and a separate recorded-contract verifier. A [fresh reliability case](reports/case-reliability-v1) ran 2026-10-30: actual MCP failover, local approval and scoped redaction in three separate experiments. Fresh operability and the complete three-case milestone remain pending. |

## Three connected case studies

1. **Permission-aware knowledge retrieval:** identity, ingestion, retrieval, and
   evaluation. Explain how identity becomes a database filter, how ACL updates flow,
   and what a stale permission can expose.
2. **Reliable legacy automation:** MCP access, durable approval, redaction, and
   failure injection. Demonstrate the distinction between retrying a request and
   safely retrying its side effects.
3. **Operable AI deployment:** isolated packaging, local inference, cost attribution,
   administration, and runbooks. Report measured local behavior and explicit limits.

## Contribution track

Prefer the libraries this lab actually uses. Start with a reproducible bug and a
focused fix, then return to the same project for review feedback. Check current
contribution rules and competing PRs before starting. No PR volume target and no
guarantee of acceptance. Track submission and merge as separate events.

## Study evidence

The 2026-10-19 [review diagnostic extension](reports/review-diagnostics-v1)
maintains study6: finite failure evidence and actual accepted-start recovery,
with no new scheduled milestone or explanation of the original intermittent CI
failure. The 2026-10-20 [privacy milestone](reports/privacy-v1) advances study8
with fictional inputs and explicit detector/storage limits. The 2026-10-21
[chaos milestone](reports/chaos-v1) advances study9 with actual read-only MCP
failure injection and bounded failover. The 2026-10-22 [fictional cost milestone](docs/cost-attribution.md)
advances study10 with exact local accounting and frozen reconciliation receipts.
The2026-10-23 [fictional activity/ROI milestone](docs/activity-roi.md) advances
study11. The2026-10-24 [offline core wheel milestone](docs/offline-bundle.md)
advances study12 with actual process network denial; it does not complete a
container/optional AI-runtime deployment. The 2026-10-25
[CPU inference milestone](docs/edge-inference.md) advances study 13 with actual
local measurements and explicit process budgets. The2026-10-26
[local configuration desk milestone](docs/admin-portal.md) advances study14 with
actual identity, bounded configuration and recovery observations. Study15 now has
a historical byte-audit milestone, a separately recorded fresh permission
workflow, and a three-part fresh reliability case with explicit separate-runtime
limits. The fresh operability workflow remains next. Modeled ROI and observed business
outcomes remain separate.

After each milestone the owner can explain the design, reproduce a result, modify
one assumption, and write a short reflection. Until that happens, the study remains
available to learn from; it is not recorded as personally completed.
