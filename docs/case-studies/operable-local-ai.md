# Local inference measurements and operational limits

The [historical CPU report](../../reports/edge-v1/README.md), recorded on
2026-10-25, asks what can be measured honestly from a bounded local model worker.
An installed 0.13.0 wheel, hash-pinned Qwen3-0.6B assets and exact runtime ran 14
requests on one macOS arm64 host: a first request and six warm requests at each
of two thread settings. The report retains all outputs, token counters, timings,
cleanup and whole-process peak RSS. A process network-denial control is recorded
separately. The [source archive](https://github.com/edsonsims/enterprise-ai-lab/tree/dd5d6b5a3f47c176e8ed3ec4482a2140307cd1fc)
preserves that publication context.

The boundary is between observation and extrapolation. First/warm timing is not
an OS-cold-cache experiment; fixed execution order does not isolate a thread-count
effect. Peak RSS is a process-lifetime highwater, not weights-only memory.
Fourteen literal-output checks are not a quality or entailment score. The
experiment establishes neither p95/p99 latency nor production capacity, hardware
superiority, a physical air gap or an optional-runtime deployment bundle.

[Core offline packaging](../offline-bundle.md), [fictional cost attribution](../cost-attribution.md)
and the [local configuration desk](../admin-portal.md) supply related operational
studies. Their integration is not implied: ledger costs are not inference
telemetry, and two fictional presentation settings are not production IAM or
system administration. This byte audit loads no model and recomputes no model
metrics. Model-free semantic verification and any explicit fresh inference use
the [edge guide](../edge-inference.md) as separate steps, with new evidence paths
and all limitations retained.

A [fresh four-part operability case](../../reports/case-operability-v1) was recorded
on 2026-10-31 from a new installed wheel. It preserves all 14 actual model calls,
864 generated tokens, first/warm timings, whole-process peak RSS and complete
outputs. Separate experiments exercise actual network-denied core installation,
fictional ledger concurrency/lost-receipt recovery and the real local OIDC
configuration desk with Chromium. Each uses new work/report paths. Optional
dependency environments are reused; the core installation creates a fresh venv.

The case records 31 ledger observations and 33 reaped CLI processes, plus 40
administration HTTP observations, seven reaped service children and nine actual
Chromium checks. Before-commit recovery advances revision 2 to 3 after one exact
retry; after-commit recovery retains revision 4. The accounting fixture remains
authored data, not usage observed from these model calls. Two view settings do
not configure the model or production infrastructure.

Its separate standard-library verifier checks recorded contracts and corruption
controls without executing these systems. This adds the third representative
case, while the wider planned deployments and the owner's personal study remain
separate.
