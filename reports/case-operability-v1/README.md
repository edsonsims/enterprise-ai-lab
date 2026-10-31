# Four local operability experiments

Recorded on **2026-10-31**, outside the source checkout, from a wheel built from
Enterprise AI Lab revision `f8afd5e2e3104147a14dfabb7d9e575140a1c211`.
The wheel's 113 package files match that committed source. Its SHA-256 is
`4d97b9a2a53639c3cfd2ca2000ffb99615c581a5ffed3d4d28f4e1a5377b8b69`.
Identity Boundary Lab is pinned at
`92a6688e6df862934d53201a6ac39d8fba410a52`.

This case asks what can be operated and measured locally, and which dependencies
remain outside each boundary. It runs **four separate experiments**. The fictional
ledger does not collect inference telemetry. The configuration desk does not
administer the model worker. The core wheel bundle does not deliver optional
dependencies, model weights or a container image.

| Experiment | Fresh observations | Boundary |
| --- | --- | --- |
| Core packaging | Same-input manifest repeatability; actual TCP/UDP positive controls and process/descendant permission denial; a new venv, installed import and original lexical demo | Trusted core wheel and host Python; no optional AI-runtime delivery or physical air gap |
| CPU inference | 14 actual Qwen3-0.6B calls, 864 generated tokens, 14 literal-format acceptances, zero generation failures, two reaped workers | Existing fixed fictional protocol; local CPU timing and process RSS, not answer quality |
| Fictional accounting | 31 observations, 33 reaped CLI processes, tenant isolation, statement anomalies, concurrent duplicate receipt and killed-after-commit recovery | Authored usage and rates, not model telemetry, provider pricing or billing |
| Configuration recovery | 40 HTTP observations, real fixture-account Code+PKCE, seven reaped issuer/resource children and nine Chromium checks | Two fictional view settings; no production IAM or model administration |

Each experiment ran once with new work and report paths. Every actual command
returned zero. The complete four raw reports are copied unchanged, including
all inference outputs and refusal/recovery observations. No retry or extra model
call followed this record. The five Python locks, prior public inputs and earlier
reports remain unchanged.

## Runtime and measurement

The experiments used Python 3.13.1 on Darwin 27.2.0/arm64. The optional runs reuse
already installed dependency environments and import the newly built wheel from
a new installation target. They are not fresh optional dependency installations.
The core network-denied experiment independently creates a fresh venv without
system site packages. [Provenance](provenance.json) retains exact distribution
inventories, locks, source bindings and actual execution times. Exact private
commands and local paths remain private; the commands below are portable templates.

The inference worker uses the unchanged Qwen checkpoint, nine asset hashes,
float32 CPU adapter and exact four neural runtime versions. Actual TCP and UDP
attempts immediately before inference returned EPERM inside the same macOS
process policy inherited by its workers. The core experiment separately records
delivered positive controls, permission-denied attempts and no observed delivery.
These process observations do not establish physical disconnection, a general
packet audit or network denial for the separate HTTP administration experiment.

In fixed order, one intraop thread has six warm observations with a median of
1.4936323755 seconds; four threads have six with a median of 1.4862099795 seconds.
The corresponding final process-lifetime peak RSS values are 4,979,195,904 and
4,980,146,176 bytes. Startup, first requests, every warm sample, generated token
counts and stderr count/hash remain separate in the [raw edge report](edge/demo.json).
Nanosecond units do not imply nanosecond measurement accuracy. Fixed order, shared
host caches, a small fictional sample and background load prevent a thread-count
effect, capacity, p95/p99, hardware-superiority or model-quality conclusion. Literal
quotes do not establish semantic entailment.

The ledger preserves its fixed fictional October 22 usage window; the new receipt
and observation times are October 31. Aggregate-first half-even rounding settles
two `0.005` lines to `0.01`; independently rounding them would incorrectly total
`0.00`. The final Alpha fixture has eight events with exact USD `0.581` and settled
`0.58`. Its lost response follows a real SQLite commit; one explicit identical
event retry returns that durable receipt. This is a local accounting contract,
with trusted launcher profiles and no pricing, payment or savings claim.

The administration faults preserve the distinction between uncertain delivery
and durable mutation. Before commit, reopening shows revision 2 and one exact
retry creates revision 3. After commit but before the HTTP receipt, reopening
already shows revision 4 and retry returns revision 4. Both need a new login after
resource restart. Rollback appends a revision. Chromium 151.0.7922.34 and the locked
Playwright 1.62.1 exercise the actual local UI. The browser fingerprint covers its
launcher only, not the entire browser distribution. Seven reaped service children
exclude the separate normal browser-driver lifecycle; no forced-timeout descendant
cleanup guarantee is inferred.

## Inspect the recorded contract

With only Python's standard library, from the source checkout:

```sh
python scripts/verify_operability_case.py --directory reports/case-operability-v1
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests/case_studies -v
```

This verifier checks recorded source/artifact bindings, counts, arithmetic,
identity refusals, immutable retries, audit/CAS relationships and cleanup evidence.
It starts no process, loads no model, logs into no account and reads no live
database. Passing it cannot establish that a fresh experiment ran or a person
reviewed it. Mutation tests keep inconsistent denial, token denominators,
tenant/accounting, receipt, revision and artifact evidence visible as failures.

The recorded bundle passes **298 contract checks**. The complete case-study suite
passes **201 tests**, including **77 new corruption and file-reading tests**. The
original **14 core tests** pass separately. These checks use Python 3.13.1 and
treat `ResourceWarning` as an error; they do not repeat the four experiments or
establish a different inference platform matrix.

- [Offline install report](offline/demo.json)
- [Complete CPU inference report](edge/demo.json)
- [Fictional accounting report](costs/demo.json)
- [Configuration and browser report](admin/demo.json)
- [Source-to-wheel inventory](wheel-source-inventory.json)
- [Portable provenance and explicit limits](provenance.json)

The finite private safety scan checks all six JSON artifacts against home/private
paths, bearer/JWT patterns and five generated fixture-password canaries, with
zero matches. Databases, account files, screenshots, issuer IPC and raw process
logs are excluded. That finite scan is not proof of absence of every possible
secret or future log leak.

## Repeat the actual experiments

Prepare the exact pinned assets and documented environments explicitly, then
install a wheel from the recorded revision. Use the [core bundle](../../docs/offline-bundle.md),
[CPU inference](../../docs/edge-inference.md), [fictional ledger](../../docs/cost-attribution.md)
and [configuration desk](../../docs/admin-portal.md) guides. The model, Python
interpreter, system libraries and browser are host prerequisites; the core wheel
does not supply them. Node 24 and the pinned Identity checkout are needed for the
administration experiment. Model and dependency acquisition are setup steps, not
runtime downloads.

Run outside the checkout, with a new report parent and new work paths:

```sh
"$BUILD_PYTHON" "$PINNED_OFFLINE_SCRIPT" --wheel "$TRUSTED_WHEEL" \
  --work-dir "$NEW_CORE_WORK" --out "$NEW_REPORT_PARENT/offline/demo.json"
/usr/bin/sandbox-exec -p '(version 1) (allow default) (deny network*)' \
  "$GENERATOR_PYTHON" -m enterprise_ai_lab.edge demo --model-dir "$PINNED_QWEN" \
  --out "$NEW_REPORT_PARENT/edge/demo.json"
"$CORE_PYTHON" -m enterprise_ai_lab.costs demo \
  --work-dir "$NEW_LEDGER_WORK" --out "$NEW_REPORT_PARENT/costs/demo.json"
"$ADMIN_PYTHON" -m enterprise_ai_lab.admin demo \
  --identity-dir "$PINNED_IDENTITY" --node "$NODE_BINARY" \
  --work-dir "$NEW_ADMIN_WORK" --out "$NEW_REPORT_PARENT/admin/demo.json" \
  --browser-test --browser-script "$PINNED_BROWSER_SCRIPT"
```

The script path in the first command denotes the matching trusted source archive;
the template does not reconstruct the separate private positive/negative control
wrapper for inference. To match this case, retain those controls and the exact
installed-import/asset inventories as separate execution evidence. Existing report
paths must never be replaced.

The unchanged ledger report retains its original component `base_head` and
`source_version` (`0.11.0`) metadata. Those identify the accounting study's
component origin. The actual new installed distribution is `0.14.1`; its committed
runtime source and 113 package-file hashes are bound independently by this case's
wheel inventory and installation readback.

These are authored fictional fixtures and automated account actions. No customer
result, production deployment, registry release, human review or owner study is
established. This third representative case leaves the broader planned scopes of
the individual studies explicit.
