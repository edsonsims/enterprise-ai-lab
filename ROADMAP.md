# Enterprise AI engineering study roadmap

The sequence balances applied AI evaluation with identity, data, reliability, and
operations. It adapts the 15 project prompts in [Suraj Sharma's October 17, 2026
post](https://x.com/suraj_sharma14/status/2111427008295301488). These are study
directions, not evidence of employability, enterprise adoption, or research novelty.

Work advances by one substantive milestone per daily session. A project may need
several sessions. A finished milestone needs runnable code, relevant tests, a clear
explanation, and evidence of what was actually tested. Integrations remain planned
until exercised against the real dependency. The three eventual case studies share
these components; separate repositories are useful only when a module stands alone.

| Order | Study (source item) | Required evidence before completion | Status |
| --- | --- | --- | --- |
| 1 | Customer data evaluation harness (10) | Strict dataset/prediction contract, baseline comparison, per-case results, reproducibility and limitations | Offline synthetic milestone implemented 2026-10-17; real-data validation pending |
| 2 | Technical discovery to SOW translator (11) | Fictional brief, explicit assumptions, acceptance criteria, scope and architecture; no invented client | Planned |
| 3 | Enterprise SSO and RBAC bridge (1) | Local standards-based identity provider, token validation, revocation/role tests; SAML and SCIM tracked separately | Planned |
| 4 | Permission-filtered RAG engine (4) | Authenticated identity to query filter, vector backend, tenant/role isolation and retrieval-quality tests | Lexical fixture baseline included in study 1; full integration planned |
| 5 | Legacy ETL and OCR ingestor (3) | Licensed scanned fixtures, provenance, idempotency, OCR error analysis and permission propagation | Planned |
| 6 | Durable human approval gate (7) | Temporal restart/replay tests, expiry, idempotency, forged approval rejection; local notifier first | Planned |
| 7 | Legacy MCP gateway (5) | Official protocol client/server interoperability, least-privilege DB access, allowlisted operations and audit | Planned; SQLite fixture before any Oracle/SAP claim |
| 8 | PII redaction and restoration proxy (6) | Synthetic test corpus, precision/recall, scoped token mapping, collision and log-leak tests | Planned; cannot promise universal PII detection |
| 9 | Chaos and failover suite (13) | Injected timeouts, bounded retries, failure recovery and measured degradation | Planned |
| 10 | Multi-tenant cost attribution (14) | Usage ledger, reconciliation, decimal arithmetic, tenant isolation and anomaly cases | Planned; fixture rates explicitly labeled |
| 11 | Customer health and ROI dashboard (9) | Traceable usage/cost, explicit baseline and assumptions, uncertainty and sensitivity | Planned; modeled savings separate from observed savings |
| 12 | Air-gapped deployment pipeline (2) | Repeatable offline artifact bundle, image inventory and tested egress denial in an isolated environment | Planned; no cloud spend authorized |
| 13 | On-prem edge inference node (8) | Actual supported local model, measured latency/memory and reproducible configuration | Planned; NVIDIA vLLM/TensorRT/MIG require appropriate hardware |
| 14 | Self-service admin and runbook portal (12) | Authenticated admin boundary, safe config updates, audit and recovery exercise | Planned |
| 15 | Three public deployment case studies (15) | Reproduce and explain the trust boundary, reliability, and measurement workflows below | Planned; lab deployments clearly labeled |

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

After each milestone the owner can explain the design, reproduce a result, modify
one assumption, and write a short reflection. Until that happens, the study remains
available to learn from; it is not recorded as personally completed.
