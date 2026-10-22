# A second read is another attempt

Study 9 asks what a caller can safely do when a local MCP connection disappears.
The small answer here is: try **one other fixed replica**, for an allowlisted
read, while keeping the same profile, dataset and absolute deadline. A tool
refusal is an answer, not a reason to seek another server.

This client is intentionally bound to the existing **authored synthetic
work-order fixture**. It uses the actual official MCP 2.3.0 client and separate
stdio gateway processes. It is not a generic retrier for arbitrary tools, a
replication system or a production availability demonstration.

## Reproduce the experiment

Use Python **3.12+ on macOS/Linux** and the unchanged MCP lock. No model, issuer,
network service, Docker, credentials or new dependency is required.

```sh
python3.13 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r requirements-mcp.lock
.venv-mcp/bin/python -m pip install --no-deps -e .
.venv-mcp/bin/python -m pip check

.venv-mcp/bin/python -m enterprise_ai_lab.chaos demo \
  --work-dir .local/chaos-demo --out reports/local/chaos-demo.json
.venv-mcp/bin/python -m pytest tests/chaos -q
```

The work directory and report must be new. Installation is explicit; execution
never downloads packages or data. [The recorded run](../reports/chaos-v1) keeps
all cases, configured budgets, observed timings, process evidence, source hashes
and both audit chains. Earlier gateway and other study reports are unchanged.

If the current demo fails, the CLI keeps exit status 2 and adds a bounded JSON
line to stderr. It identifies a fixed case/stage, a fixed exception category,
allowlisted outcomes/codes and at most two attempts. No exception message,
traceback, custom class name, path, ID, text, environment or raw trace is included.
An unavailable/malformed attempt count is `null`, rather than an invented count.
There is no diagnostic file or broad work-directory upload. A failed diagnostic
callback cannot replace the original programmatic exception, and caller
cancellation still propagates without failover.

These failure-only diagnostics were added after a Linux standalone demo exited
with only a generic message. Its stage and cause remain unknown: elapsed time,
a passing test run or a later successful demo do not establish what failed.
The frozen original report predates this diagnostic path; its exact matching
source is still required for its source fingerprints.

## What can fail over?

| Observation | Policy |
| --- | --- |
| Official SDK request timeout or verified transport closure | At most one read on the fixed secondary, if total work time remains |
| `forbidden`, `not_found`, `invalid_request` | Terminal; no secondary attempt |
| Source busy, audit unavailable or internal tool error | Terminal; no audit-fence bypass |
| Unexpected catalog, malformed output, wrong tenant/row or source drift | Terminal; withhold data |
| Caller cancellation | Propagate cancellation; no secondary attempt |
| Both transports fail or total work deadline expires | Stop; no hidden third attempt |

Only `list_work_orders`, `get_work_order` and `get_asset_diagnostics` are accepted.
There is no SQL, path, connection string, write, action or caller-supplied role
operation. One trusted launcher profile applies to both endpoints. This is local
operator policy, **not authentication**; a host administrator remains trusted.

The default budget is four seconds of total work, two seconds per attempt and
at most two attempts. An attempt includes process startup, protocol negotiation,
catalog discovery and one tool call. Its absolute deadline is the earlier of the
remaining total and its own per-attempt deadline. Cleanup time consumes the
remaining total before a second attempt can start; it never resets that total.
A success arriving or accepted after its deadline is refused.

Cleanup has a different obligation: do not abandon an owned child because the
caller stopped waiting. The SDK shields its bounded transport shutdown. A
private PID record plus an independent parent/command observation restricts
termination to the child created for that attempt. A watchdog kills that owned
child on deadline or cancellation; normal EOF shutdown has a short grace.
The report measures work, cleanup and whole-read elapsed time separately.
**A work deadline is not a hard real-time promise about whole-call return.**
Scheduling, OS process operations and mandatory cleanup have their own cost.

## Why these replicas are interchangeable

Admission reads the complete, public synthetic inventory and row grants from
each read-only SQLite copy and compares them with the packaged fixture. Matching
provenance metadata alone is insufficient. The client then seals each source's
byte hash and rechecks it before each attempt and before accepting a result.
The source/audit main, WAL, SHM and journal families must be disjoint, including
inode aliases across endpoints. Audits remain mutable and are independently
validated by the existing gateway before and after each business read.

The official wire catalog must match the fixed profile. Returned text and
structured content must agree, match the strict output schema and equal the
fixture-authorized result for that exact named operation. This deliberately
narrow reference check preserves tenant/row-role permissions and catches a
schema-valid foreign-tenant response. It does not establish freshness or
replication consistency for a changing enterprise database. New data would need
a new contract, not a hash-update shortcut.

## Read the injected failures

The experiment-only launcher instruments the genuine gateway process. Its
fault modes are fixed in the demo; the read client exposes no fault selector or
arbitrary command. The old gateway's service, SQL, schemas and audit protections
remain unchanged.

- **Healthy baseline:** one primary read through actual MCP.
- **Kill after durable outcome:** the real read and both audit writes finish,
  then `SIGKILL` prevents the response. The secondary performs another audited
  read. Both distinct attempts remain visible.
- **Disconnect before the read:** the child exits after negotiation but before
  entering its business service; one secondary read succeeds.
- **Kill after intent:** the real intent commits, then the process dies before
  its source query. Recovery preserves that unfinished intent.
- **Stall after the read:** a bounded test-only delay withholds a completed
  response until the work budget expires. Both-failure and total-deadline cases
  retain their failures and attempt counts.
- **Terminal controls:** permission denial, unreadable row, attempted tenant
  override, actual SQLite source/audit locks and a schema-valid wrong-tenant
  wire response all stop after the primary.
- **Explicit recovery and cancellation:** a fresh healthy primary read preserves
  its result and old audit prefix. Separately, the parent cancels after a child
  marker proves a durable outcome; no secondary starts and the audit remains.

The client does not inspect an audit and delete evidence to make a replica
available. A surviving intent is not proof that SQL ran, and a timeout is not
proof that it did not. The controlled post-read cases prove that a response may
be lost after work has completed. Repeating reads leaves source data unchanged
while producing additional audit attempts. There is no exactly-once, rollback,
aborted-SQL or cross-database transaction claim.

The healthy/kill/stall timings are single local observations including startup,
negotiation and cleanup. They measure these runs, not a latency percentile,
failure probability, service-level objective or general performance improvement.
Both replicas share one trusted host; this does not exercise host/disk failure,
network partitions, remote identities or a production failover topology.

## Study exercises

1. Follow the killed-after-outcome case through both audit chains. Explain why
   two matching output digests do not mean one execution.
2. Change a budget in a disposable run. Predict the second attempt's remaining
   time, then compare it with the controlled-clock test and recorded process run.
3. Hold an audit lock. Explain why trying another replica would bypass a refusal
   even though this particular business operation is read-only.
4. Compare a surviving intent with a completed outcome whose response was lost.
   State what each proves and which facts remain unknown to a normal caller.
5. Design a write-tool variant on paper. Identify authorization, durable
   idempotency, reconciliation and approval obligations before allowing retries.

These exercises are available to study; no owner completion or human review is
recorded by automated logins, code execution or tests.
