# Durable local approval: process evidence

[demo.json](demo.json) records **29 HTTP observations and five actual Temporal
histories**. The run uses Python 3.12.14, Temporal Python SDK 1.34.0, the pinned
CLI 1.9.1 / native server 1.32.0, Node 24.19.0 and the actual local OAuth issuer.
Only synthetic local notes are used; no language model or external notifier runs.

| Boundary | Observed behavior |
| --- | --- |
| Repeated request ID, unchanged content | Same immutable request |
| Repeated request ID, changed content | 409 |
| Forged JWT / RAG-audience JWT | 401 |
| Wrong tenant, role or scope | 403 |
| Changed snapshot / self-approval | 409 |
| All server, worker and HTTP processes restarted while pending | Same pending request; no local effect |
| Worker killed after SQLite commit, before activity acknowledgement | Temporal retries on attempt 2; one completed activity and one Alpha notification row |
| Same decision repeated | Prior receipt, one effect |
| Same command ID with different verdict, actor or digest | 409 |
| Same request ID in Beta | Separate snapshot and one Beta effect |
| Rejected request | No notification |
| Deadline passes while worker is absent | Expired; late decision 409 |
| HTTP edge restarted again | Published state remains readable |
| Five exported histories replayed with the actual SDK Replayer | No nondeterminism failure; no additional local effect |

The two notification rows are the approved Alpha and Beta notes. Rejected,
self-review and expired requests do not add rows. The raw histories are in
[histories/](histories/); their complete-file hashes, event counts and event types
are in the report. The Alpha history retains an activity start with `attempt: 2`
and a previous StartToClose timeout after the worker was killed. Replayer uses
recorded activity results; it does not publish notes again.

Approval and publication are separate fields. A successful decision receipt can
be returned while its local effect remains pending. If an HTTP or RPC deadline
expires, read status or retry the **same IDs and immutable body**; creating a new
ID is not an automatic retry policy.

These role-bearing clients simulate a reviewer. **No actual human authentication,
owner review or human approval is recorded.** The HTTP edge validates dedicated
JWTs; the loopback development gRPC endpoint remains trusted administration and
can bypass that edge. This is not a production identity or workflow deployment.
Temporal history retention bounds workflow deduplication; local-effect deduplication
also depends on preserving its separate SQLite database. No global exactly-once
claim is made.

See [setup, protocol, tradeoffs and study exercises](../../docs/durable-approval.md).
The original lexical, synthetic RAG, neural observations and PDF/OCR bundles remain
separate, unchanged experiments.
