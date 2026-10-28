# Read-only access and explicit retry boundaries

The [historical MCP failover report](../../reports/chaos-v1/README.md), recorded on
2026-10-21, asks what a read client can safely retry after losing a response.
Official MCP client/server processes read an authored fictional SQLite fixture
through fixed operations and tenant AND row-role filtering. Fifteen cases cover
transport loss, deadlines, terminal permission/audit errors and explicit recovery.
The run records 20 child processes and 31 audit rows, retaining an unfinished
intent after a killed process. Both source database hashes remain unchanged.

This makes the retry distinction concrete: a read can have committed an audit
outcome before its response disappears. A second read creates another audit
effect; it does not make the first one vanish. At most one transport failover is
allowed within an absolute work budget. Mandatory cleanup can extend elapsed
time. Permission, audit and wrong-tenant failures stop instead of moving to a
second replica. The [source archive](https://github.com/edsonsims/enterprise-ai-lab/tree/f1b419b0f193c58648834133912a5bce3ff8918a)
contains the original source context; later diagnostics intentionally do not
rewrite that report's working-tree fingerprints or explain an earlier unknown
CI failure.

Launcher profiles are trusted operator policy, not authentication. These are
local replicas on one host, not production failover or evidence of interruption
inside executing SQLite. The [privacy proxy](../privacy-proxy.md) and
[durable approval](../durable-approval.md) illustrate related boundaries but are
separate runnable studies: there is no connected privacy-to-MCP write/action
workflow. No external database or notification is exercised. A fresh run of the
[chaos guide](../chaos-failover.md) remains a separate execution with new paths;
matching archived bytes does not perform it.
