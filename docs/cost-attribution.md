# Count usage before rounding cost

Study 10 is a local accounting contract: retain usage once, attribute it to the
right tenant, and reconcile an independently authored statement against a frozen
ledger snapshot. All rates, usage and statements are **fictional scenario inputs**.
They are neither measured RAG/model/OCR telemetry nor actual provider prices,
invoices, billing, payments, savings or ROI.

## Run offline

Use Python 3.10+ on macOS/Linux. The module uses only the standard library; no
provider, model, network service, credentials or new dependency lock is needed.

```sh
python -m pip install --no-deps -e .
python -m unittest discover -s tests/costs -v
python -m enterprise_ai_lab.costs demo \
  --work-dir .local/cost-demo --out reports/local/cost-demo.json
```

Use new work/report paths. The command starts real local CLI processes over one
private SQLite database. It exercises independent tenant totals, replay/conflict,
restart, statement anomalies, two concurrent writers and a test-only child killed
after commit but before stdout. Retrying that exact event preserves its original
receipt. The fixed experiment wrapper supplies the fault; normal commands have
no crash selector, arbitrary command, SQL or provider connection option.

The [recorded run](../reports/costs-v1) binds the selected runtime/source and
fixture hashes. Original reports and locks are unchanged.

## Identity and persisted usage

`alpha-operator`, `alpha-operator2` and `beta-operator` can append/read.
`alpha-auditor` and `beta-auditor` can read/reconcile. `alpha-observer` is denied.
These are **trusted local launcher profiles, not authentication**. A person who
controls the launcher can select a profile; this is not an internet-facing API.

Every lookup, replay, aggregate and receipt query filters by the selected tenant.
Alpha/Beta deliberately share event IDs. Tenant-local ordinals avoid exposing
another tenant's activity through pagination or snapshot watermarks. Request IDs
are ordinary data and do not impose hidden uniqueness across events.

An event contains only ID, request ID, metric, nonnegative integer quantity,
version and fixed-second UTC occurrence time. Its tenant/actor come from the
launcher. The digest binds the exact event, tenant and canonical rate-card hash.
An identical retry preserves receipt ID, charge, first actor and creation time,
even through a second authorized operator. Changed immutable input conflicts
without changing usage. There are no in-place updates, credits or refunds.

## Exact amounts and one rounding boundary

The packaged [rate card](../src/enterprise_ai_lab/costs/data/rates-v1.json) assumes
USD 0.5/1,000 input tokens, 1.5/1,000 output tokens, 0.005/embedding token and
0.015/OCR page. These deliberately simple fictional values make arithmetic
inspectable; they do not represent any service or model.

JSON/SQLite money is decimal **text**, never binary float or SQLite numeric
aggregation. Quantities are at most 10^9; positive prices have at most 12
coefficient digits and six fractional places; unit denominators are powers of ten
up to 10^6. At most 1,000 events per tenant need at most 36 common-scale digits,
including aggregate carry. A fresh precision-50 Decimal context traps unintended
rounding and is independent of caller flags, traps and precision. Only aggregate
cents settlement deliberately rounds, using half-even.

| Exact total | Settled total | `settled - exact` |
| --- | --- | --- |
| `1.005` | `1.00` | `-0.005` |
| `1.015` | `1.02` | `0.005` |
| Two `0.005` lines: `0.01` | `0.01` | `0` |

Individually rounding those two tiny lines would incorrectly sum to `0.00` under
this contract. Exact strings omit redundant zeros; settled strings have exactly
two decimal places. Exponents, signed/negative-zero prices, floats, ambiguous
times, duplicate JSON keys and identity overrides are rejected.

## A statement is a frozen result

A statement declares its own ID, currency, half-open `[start,end)` UTC interval,
unique event/amount lines and two-decimal total. Reconciliation and receipt
registration share one SQLite `BEGIN IMMEDIATE` transaction with appends.
The registry binds tenant/statement ID, canonical input, rate card, tenant-local
watermark, complete snapshot digest and result. The same ID/input returns its
**original** result after restart or later events; a new ID evaluates a newer
snapshot. Changed input under an existing ID conflicts.

Missing means a ledger event absent from the statement; extra means a declared
event absent from this tenant's snapshot. Wrong line amounts, missing/extra rows,
currency and settled total remain explicit mismatches. Duplicate statement lines
are rejected before any total or receipt. A different currency suppresses
cross-currency amount/total comparison: there is no FX conversion. Computed
reconciliation totals retain their explicit rate-card currency (`USD`), including
when the statement declares a different currency.

## Storage costs and limits

Opening verifies the exact schema/triggers and rate-card binding before writable
settings. Reads and writes revalidate selected-tenant charges, digests and frozen
statement results. SQLite money remains TEXT. Each writer enforces a 32 MiB page
cap; capacity and a bounded busy wait refuse work rather than truncate results.
There are at most 100 statement receipts/tenant, 100 rows/page, 256 KiB input and
512 KiB output. No automatic retention, migration or history deletion exists.

Regular-file/mode/owner/link and captured inode rechecks reject the exercised
unsafe/replaced paths. Active database admission uses no raw database descriptor
closes, which could release SQLite's process-wide POSIX locks. The host and
filesystem remain trusted; these checks are not a hostile-host race-proof sandbox.
Schema/digest checks are not an externally anchored or administrator-proof audit.

SQLite may naturally remove a sidecar during a concurrent commit. Admission
tolerates only a missing non-main file at that stat boundary; missing/replaced
main files and existing unsafe sidecars still fail closed. A real child-commit
barrier regression covers this separately from the unchanged
[original report's follow-up note](../reports/costs-v1#journal-admission-follow-up), whose
source hashes precede that repair. The original Linux CI's unprinted child error
does not establish its historical cause.

The tradeoff is deliberate: exact verification and immutable receipts cost reads,
storage and serialized writes. This small local ledger does not establish
production throughput, high availability or a billing integration.

## Study exercises

1. Explain why two `0.005` events settle differently when rounded separately.
2. Change a fictional rate under the same version in a disposable copy. Explain
   why an existing ledger refuses the changed card instead of repricing history.
3. Append a later event after reconciling. Compare replaying the original statement
   ID with submitting a new ID, and identify each snapshot watermark.
4. Follow the killed child through the recovered receipt. State what commit,
   stdout delivery and replay each prove, without claiming end-to-end exactly once.
5. Design credits, real usage collection and authenticated access on paper. Keep
   those requirements separate from the implemented local contract.

Exercises are available to study; execution does not establish owner study or
human review. [Decimal semantics](https://docs.python.org/3.10/library/decimal.html),
[SQLite transactions](https://www.sqlite.org/lang_transaction.html) and
[SQLite POSIX locking cautions](https://www.sqlite.org/howtocorrupt.html#_posix_advisory_locks_canceled_by_a_separate_thread_doing_close_)
explain the underlying primitives; the numeric bounds above are this lab's contract.
