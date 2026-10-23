# Separate activity from modeled benefit

Study 11 connects the existing fictional cost ledger to a local activity and ROI
scenario dashboard. SQLite transactions, CLI processes, tenant filtering and file
exports are real. **Usage, prices, labor assumptions and all calculated business
benefits are fictional.** No customer, successful task, measured savings, actual
ROI, forecast quality or model inference is established.

## Reproduce offline

Python 3.10+ on macOS/Linux, standard library only:

```sh
python -m pip install --no-deps -e .
python -W error::ResourceWarning -m unittest discover -s tests/health -v
python scripts/health_demo.py \
  --work-dir .local/health-demo --out reports/local/health-demo.json
```

Use new work/report paths. Open `.local/health-demo/alpha/index.html` locally.
The page contains inline CSS, no executable scripts or external assets, named
keyboard-focusable scrolling tables and a content policy denying network loads.
No HTTP server, model, provider telemetry, authentication or credential is needed.

To render an existing study ledger with an authored scenario:

```sh
python -m enterprise_ai_lab.health render \
  --ledger .local/health-demo/ledger.sqlite --profile alpha-auditor \
  --scenario public-data/health-v1/scenario.json --out-dir .local/another-view
```

Existing output directories are refused. The CLI creates a new 0700 directory
and 0600 `report.json`/`index.html`. An interrupted export may leave an incomplete
directory; inspect it and use a new path. This is not a transaction across SQLite
and multiple exported files. Plaintext exports carry no live tenant authorization:
anyone given the files can read them. Do not publish exports from private ledgers.

## One snapshot, two comparable windows

`CostLedger.snapshot` invokes the existing authorized read once. One SQLite
transaction validates the selected tenant's usage and immutable statement receipts.
It returns a tenant-local watermark and a digest of the full selected receipt rows,
with compact event references. Both adjacent equal-duration half-open UTC windows
are computed from that same snapshot. The dashboard never paginates across moving
ledger reads, mixes currencies or lets a scenario choose a tenant.

Each reference retains event ID, request ID, metric, quantity, time, exact charge,
receipt ID, event digest and ordinal. Recomputing the selected-full-row digest
requires the preserved **full ledger receipts**; compact report references alone
are insufficient. Runtime fingerprints, scenario hash, canonical rate-card hash,
watermark and source references support inspection; they are not a signature,
externally anchored audit or guarantee against a privileged operator.

The activity counts are exactly what the authored ledger contains: events and
unique request IDs, with quantities/charges per metric. Multiple token/OCR events
can belong to one request. Requests are not assumed successful or completed.
Three explicit flags compare current usage to zero, a declared minimum and the
previous count. They are rules over fictional activity, not a learned or validated
customer-health score. Percentage change is undefined when previous usage is zero.

The [authored fixture](../public-data/health-v1) has three previous requests and
two current requests for Alpha. Its three current events cost fictionalUSD 0.83;
Beta deliberately reuses an event ID with a different quantity and request marker.
The local story checks that Beta's fictionalUSD 50.00 does not enter Alpha's report.

## Exact scenario arithmetic

The input declares ordered low/base/high triples for baseline minutes per request,
automated share and realized-value share, plus hourly rate, fixed cost allocated
to this window and operating cost. These are explicit scenario assumptions, not
estimates inferred from tokens or observations. All numeric inputs are bounded
canonical decimal text; floats, NaN, duplicate/extra keys, negative values,
out-of-order ranges and tenant/currency overrides are rejected.

For each of the 27 combinations:

```text
modeled gross = current request count × baseline minutes × automated share
                × realized-value share × hourly rate / 60
modeled cost  = exact current fictional ledger charges + allocated fixed + operating
modeled net   = gross - cost
modeled ROI%  = net / cost × 100, when cost > 0; otherwise undefined
```

Fractions preserve exact numerators/denominators, including repeating divisions by 60.
Only display amounts/percentages round to two places using integer half-even
rounding. No binary-float arithmetic or ambient Decimal context chooses results.
There is no per-event rounding, statistical confidence interval, probability,
Monte Carlo sample or estimated causal effect. The low/high rows bound only the
authored monotone nonnegative input ranges; they do not bound real outcomes.

In the base fixture, two request IDs ×15minutes ×0.5 ×0.5 ×USD 40/hour /60 gives
modeled gross USD 5.00. Fictional ledgerUSD 0.83 + fixedUSD 3 + operatingUSD1 gives
cost USD 4.83, net USD 0.17 and displayedROI 3.52%. A zero modeled benefit yields
negative100% ROI when cost is positive; zero cost remains undefined.
No business result was measured to produce those values.

## Read the evidence and modify an assumption

The [recorded local run](../reports/health-v1) retains actual CLI responses,
source/fixture hashes and separate Alpha/Beta/later reports. Repeating an export
without changing inputs reproduces its bytes; a later append produces a new
watermark/report while the earlier files remain unchanged. The demo denies
Python socket-connect/DNS audit events in each child; that is not an OS sandbox.
Existing cost storage, concurrency, failure and retention limits remain in force.

1. Explain why three current events identify only two usage requests.
2. Set realized-value share to zero in a disposable scenario. Compare gross,
   net and ROI, and state why this is a model change rather than observed savings.
3. Change the declared minimum without altering usage. Explain why a different
   activity flag does not establish a different customer outcome.
4. Append an event after export. Reconcile the two tenant-local watermarks and
   identify which full receipts are needed to recompute each snapshot digest.
5. Propose real task completion/outcome measurements and a credible baseline.
   Keep that future study separate from this fictional arithmetic exercise.

Exercises are available; no owner study or human review is inferred. Real usage
collection, successful-task telemetry, causal evaluation, authenticated serving,
real pricing, customer integrations and production operations remain unimplemented.
