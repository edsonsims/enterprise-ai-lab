# One local configuration and recovery run

Recorded 2026-10-26 at12:48:47UTC on macOS26.6.2/arm64, Python3.12.14,
SQLite3.53.1 and Node24.19.0. The0.14.0 wheel was built locally, installed into a
fresh exact dependency environment, and run outside the checkout with PYTHONPATH
removed. Runtime source bytes and packaged HTML/CSS/JS matched the checkout.
The separate Identity Boundary Lab checkout is pinned at
`92a6688e6df862934d53201a6ac39d8fba410a52`.

## Observations

The process experiment records40 HTTP observations over18 authored fictional
work orders. Actual Authorization Code+PKCE login establishes dedicated signed
user identities. It exercises tenant-local configuration, row-role filtering,
auditor/observer restrictions, Origin/CSRF refusal, stale-revision/body conflicts,
exact receipt replay and rollback into a new revision. The source database hash
is unchanged before and after the experiment.

| Controlled interruption | Revision after reopen, before retry | Revision after one explicit identical retry |
| --- | ---: | ---: |
| Before SQLite commit | 2 | 3 |
| After commit, before HTTP receipt | 4 | 4 |

The first interruption leaves no partial change. The second leaves a durable
receipt despite the lost response; retry returns it without reapplying the
configuration. Both resource restarts invalidate the old session and require
login again. The process report contains five distinct committed core receipts
across two tenants. The later browser exercise adds two alpha revisions; those
are separate from the frozen HTTP-observation receipt count.

All seven issuer/portal service children were reaped. That count excludes the
separate browser driver/Chromium lifecycle and is not a descendant-cleanup claim
for a forced browser-driver timeout. The actual normal browser run closes its
browser and passed eight checks on Chromium151.0.7922.34/Playwright1.62.1: login,
keyboard confirmation, rollback, account-bound pending-command reload, auditor
read-only controls, tenant isolation, a390px viewport and no uncaught app errors.
Rendered desktop/mobile screenshots were inspected privately; this is not an
accessibility certification or owner-performed review.

## Validation scope

The full installed-package test run passed911 cases and575 subtests, with82
optional integration cases disabled. It retained one pre-existing resource warning:
the old PDF extraction timeout path leaves a child stdin pipe unclosed. A scoped
allocation trace reproduced the same warning against the previous source commit;
it did not run admin code. This run does not claim a warning-free full suite.
After the final import-order-only cleanup, all97 admin cases and91 subtests passed
again with actual OIDC/process/Chromium integration and resource warnings treated
as errors. Repeats are not additional unique cases. The original14 lexical tests
and all12 fresh lexical reference files remain unchanged.

## Files and reproduction

- [`demo.json`](demo.json): original process/browser observations and source hashes.
- [`execution.json`](execution.json): installed-artifact, host and command evidence.
- [`manifest.json`](manifest.json): hashes/sizes of both JSON artifacts.
- [Protocol, setup, runbook and exercises](../../docs/admin-portal.md).

```sh
python -m enterprise_ai_lab.admin verify-report \
  --report reports/admin-v1/demo.json --identity-dir ../identity-boundary-lab
# A new actual run needs fresh paths and the exact setup in the guide:
python -m enterprise_ai_lab.admin demo \
  --identity-dir ../identity-boundary-lab --node node \
  --work-dir .local/admin-new --out reports/local/admin-new.json \
  --browser-test --browser-script tests/admin/browser.mjs
```

The offline verifier checks current source fingerprints, bounded report structure
and the internal consistency of recorded receipts/faults; it does not prove that
a process ran or a person reviewed the change. Use the matching source checkout
to verify this frozen run. Dependencies and Chromium were installed explicitly
before the experiment. The experiment used local assets and loopback HTTP; this
run does not measure OS network denial or a physical air gap.

Only page size and default status change. No permission/IAM mutation, system
command, process restart control, model/provider call, production deployment or
registry release is implemented. SQLite provides one local atomic transaction;
its hash-linked audit has no external anchor against the trusted host. No global
exactly-once delivery, customer outcome, human study or model-quality result is
claimed. Existing reports, public inputs, artwork and dependency locks are unchanged.
