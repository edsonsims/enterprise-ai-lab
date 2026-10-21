# Local MCP failure and failover evidence

[demo.json](demo.json) is a real local run using the official MCP SDK 2.3.0,
Python 3.13.1, SQLite 3.48.0 and macOS arm64. Actual server metadata reports
Enterprise AI Lab 0.9.0; each child negotiated protocol 2026-07-28. The report
binds the chaos/gateway source files, packaged synthetic fixture and unchanged
MCP lock by SHA-256. These are working-tree source fingerprints, not a claim
that an earlier base commit already contained this new study.

Recorded 2026-10-21T12:18:00.081626Z; report SHA-256:
`3f87283d445c2a9d7d4ee0227ea314f5e92d995ffb60b85a8086ce5d42b73a21`.

The run records **15 cases, 20 actual owned child processes and 31 audit rows**
(23 primary, 8 secondary). Every observed child was absent after the official
transport exited. Both source databases retained their byte hashes, and both
complete audit chains validated. One unfinished intent remains in the primary
chain after a deliberately killed process; later successful reads do not remove
it or manufacture an outcome.

| Case | Observed result |
| --- | --- |
| Healthy primary | One read; one complete audit pair |
| Primary killed after durable outcome | One secondary read; two distinct complete audit attempts for the same input |
| Primary disconnects before reading | Secondary succeeds; primary has no audit attempt |
| Primary killed after intent | Unfinished primary intent survives; secondary succeeds |
| Primary stalls after reading | Work timeout; one secondary read |
| Both transports disconnect | Two attempts exhausted; no third call |
| Absolute total work deadline | One attempt; secondary never starts |
| Permission, unreadable row, tenant-override or actual source lock | Terminal tool error; no secondary |
| Actual audit write lock | Terminal audit failure; no secondary or new audit intent |
| Schema-valid wrong-tenant wire response | Terminal provenance mismatch; data withheld |
| Explicit fresh primary recovery | Same authorized response; one new complete audit attempt |
| Caller cancellation after durable outcome | Cancellation propagates; no secondary; completed audit pair remains |

Kill, disconnect, stall and result mutation are **test-only injections** around
the genuine gateway service. Source/audit locks use real SQLite connections.
The killed-after-outcome and cancelled-after-outcome cases explicitly show that
a missing response does not prove the read failed. This is read-only business
data; additional audit writes are still effects. No exactly-once, rollback or
interrupted-SQL claim is made.

Each normal observation includes total/per-attempt work budgets, attempt limit,
work/cleanup/whole-read elapsed time, negotiated server metadata and audit
changes. A work deadline stops accepting results; mandatory cleanup can extend
whole-call elapsed time. The single healthy/kill/stall measurements include
process startup and protocol negotiation. They are local observations with
sample size 1 each, not latency percentiles, failure probabilities or a performance
guarantee. Before-handshake startup failures are not among these retained cases;
their work/cleanup components have not been separately measured here.

The fixture is authored fictional work-order data on two copies on one trusted
host. A launcher profile selects tenant and roles; it is not login. There is no
remote topology, hostile-upload boundary, model/provider, external database or
write/action tool. The exact fixture-matching client does not claim live replica
freshness or general MCP-provider compatibility.

Reproduce from the matching source with the unchanged `requirements-mcp.lock`:

```sh
python -m enterprise_ai_lab.chaos demo \
  --work-dir .local/chaos-demo --out reports/local/chaos-demo.json
python -m pytest tests/chaos -q
```

The final affected-source suite passed **77 tests**, with controlled-clock/file
checks separate from the actual official-client process fixture. This does not
substitute for other project or platform integrations.

Use a new work directory/report. [Design and study exercises](../../docs/chaos-failover.md)
explain the trust, deadline and audit boundaries. Earlier gateway/RAG/approval
reports and public input files remain unchanged. Tests and recorded executions
do not establish owner study or human review.
