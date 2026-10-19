# Local SQLite MCP — recorded interoperability and failure evidence

This is an **actual local process run on 2026-10-19**, not a model evaluation or
enterprise deployment. [demo.json](demo.json) retains the complete synthetic
responses, schemas, metadata-only audit and observed runtime/protocol details.
It was captured from source commit
`40071b5af0a6174432ccdac8c2ad48ab31579647`; its source fingerprints remain frozen.
The fixture and profiles are explicitly authored policy, not authentication or
customer data. No Oracle/SAP system, model or remote identity provider is called.

| Observation | Recorded result |
| --- | --- |
| Source data | 18 fictional work orders and six diagnostics; Alpha/Beta colliding IDs |
| Official SDK | Python `mcp==2.3.0`, `mcp-types==2.3.0` |
| Client runtime | Python 3.13.1, SQLite 3.48.0, macOS arm64 |
| Server runtime | Independently computed fingerprints received in server instructions, retained per session |
| Wire | Actual modern discovery `2026-07-28`; separate legacy initialize `2025-11-25` |
| Process sessions | Eight real stdio child processes, all observed reaped after context exit |
| Checks | 32 observations: actual tool calls plus labelled controlled faults/engine probe |
| Reader pages | `0001,0003` → `0005,0006` → `0008,0009`; tenant/role predicate precedes page limit |
| Restricted records | Reader receives the same `not_found` for missing and hidden IDs; analyst can read the granted row |
| Diagnostics / observer | Reader direct diagnostic call refused; observer empty catalog and direct read refused |
| Injection/coercion | Tenant/role/SQL fields, invalid status/ID and bool/string/oversized limits refused |
| Persistence | Fresh server process returns the same source result and retains the audit prefix |
| Main audit | 58 intent/outcome events; full chain validated; no row bodies or raw arguments |
| Source file | Exact database SHA256 unchanged across the complete run |

An audit-write lock and corrupted audit refuse the call before a durable intent,
so neither becomes a success entry in the main audit. The corruption probe uses
a **copy**, deliberately bypasses an append-only trigger as a trusted test, then
restores the exact schema. It establishes corruption detection, not protection
against an administrator capable of replacing the whole chain.

Other controlled probes hold actual SQLite locks or call the underlying
read-only engine directly. No arbitrary-SQL operation is exposed on MCP.
Normal protocol observations are labelled `actual_stdio`; injected cases are
`controlled_fault` or `controlled_engine_probe`.

## Reproduce

From the repository root, on macOS/Linux with Python 3.12+:

```sh
python3.13 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.lock
.venv-mcp/bin/python -m pip install --no-deps -e .
.venv-mcp/bin/python -m enterprise_ai_lab.mcp_gateway demo \
  --work-dir .local/gateway-reproduction --out reports/local/gateway-reproduction.json
.venv-mcp/bin/python -m pytest tests/gateway -q
```

Use a fresh directory. Runtime never downloads; explicit dependency installation
is the bootstrap. Timestamps, call IDs, audit hashes and timings vary on rerun.
Do not compare the JSON byte-for-byte; compare outcomes, source hashes, permitted
IDs and chain invariants. Observed timings are diagnostics, not a performance
benchmark. The source JSON SHA is
`7f37ae73b4c670f22276e43170517b5c30a6ecaadbb7aba07231d20e271def56`.
The report also fingerprints the exact gateway source files used.
The subsequent Linux portability repair requests unlimited-width `ps` output
when observing long server commands. It changes process observation, not tool
results; current source fingerprints therefore differ from the preserved run.

The focused suite adds actual SDK timeout, caller-deadline and cancellation
tests, including cancellation after observing a committed remote intent,
same-client recovery and process reaping. The cancellation case uses a labelled
test-only pre-query barrier to control scheduling before the real SQL runs;
the separate source-lock cases exercise actual SQLite busy waits. A timeout means the caller stopped
waiting; it does not mean a source read or audit commit never ran. Other tests
exercise an outcome-write failure after the real source read and preserve an
honest intent-only gap. Those cases are test evidence, not extra observations
silently added to this frozen demo.

## Limits

The launcher/host, source schema and filesystem are trusted. Tenant/role checks
are real SQL/service checks over authored profiles. The source is read-only;
there are no write, approval, Oracle/SAP or remote-IAM tools. Audit has a finite
10,000-event bound and no external anchor; it is separate from the source, not
a distributed transaction. Model quality, customer outcomes, production
security, owner study and human review are unclaimed. Existing RAG/OCR/approval
and browser-review reports remain unchanged.

[Design, tradeoffs and exercises](../../docs/sqlite-mcp-gateway.md) ·
[Fixture provenance](../../src/enterprise_ai_lab/mcp_gateway/data/README.md) ·
[AI assistance](../../AI_ASSISTANCE.md)
