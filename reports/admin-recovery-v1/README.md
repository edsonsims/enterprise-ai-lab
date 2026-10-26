# Configuration readiness and finite failure evidence

Recorded2026-10-26 at13:21:04UTC from a locally built0.14.0 wheel installed outside
the checkout, with PYTHONPATH removed. Python3.12.14/SQLite3.53.1, Node24.19.0 and
Chromium151.0.7922.34/Playwright1.62.1 use the same explicit setup and pinned
Identity commit as the [original frozen run](../admin-v1). Its four files remain
unchanged; matching source is required to verify that earlier report.

## What prompted the follow-up

The first remote run at source`aeed36807a615db769cbfcd5d8ce418a0c0e792d`,
[CI40326281678](https://github.com/edsonsims/enterprise-ai-lab/actions/runs/40326281678),
finished with25 successful jobs, one failed admin Python3.12 job and one cancelled
Python3.13 counterpart. The failed job passed90 cases and87 subtests before seven
shared integration-setup errors: the Chromium child returned nonzero. The initial
harness discarded its private failure report, so its browser phase and cause are
unknown. A later green run cannot establish that historical cause.

A separate deterministic probe held an actual successful configuration response
before delivery. The old UI exposed an empty revision and enabled input while its
snapshot was null. Delivering exactly the same bytes populated the real view.
The desk now remains hidden until the initial configuration, permitted work-order
page and audit finish loading. The permanent browser regression holds one real200
response, checks the hidden view, and then delivers its unchanged body. It does not
fabricate API results. Receipt checks also wait for the existing asynchronous view
refresh to finish before issuing their next inspection request.

Browser failures now expose only a fixed code, a whitelisted stage and an observed
bounded integer return code. The private report reader permits one owned0600
regular single-link file, no symlinks/FIFOs, bounded to32KiB with strict JSON.
The diagnostic exception contains no raw child stdout/stderr, exception text,
credentials or private URLs. No private runtime directory or raw browser report is uploaded by CI.
The existing15-second browser condition and90-second driver budget are unchanged;
no automatic mutation retry or relaxed identity/CSRF check was added.

## Recorded result and limits

All136 current admin cases and91 subtests passed with actual OIDC/HTTP/SQLite and
Chromium enabled, treating resource and unraisable warnings as errors. This is a
focused follow-up, not a claim that every optional integration was repeated locally.
The earlier full installed run's911 cases/575 subtests,82 disabled integrations
and pre-existing PDF timeout-pipe warning remain separately recorded.

The new report retains40 core HTTP observations, five core committed receipts,
two controlled commit-boundary interruptions and seven reaped issuer/portal service
children. The browser's nine checks include the labelled real-response delay and
add two alpha revisions after the core observations. As before, the source fixture
hash is unchanged; original exact-body retry and append-only rollback semantics
remain intact. Browser-driver/Chromium timeout descendant cleanup is outside the
seven-service-child count. Automated account actions establish neither human review
nor an accessibility certification.

- [`demo.json`](demo.json): new unmodified observation report and source hashes.
- [`execution.json`](execution.json): installed artifact and follow-up provenance.
- [`manifest.json`](manifest.json): hashes/sizes of both JSON files.
- [Setup, contract and recovery exercises](../../docs/admin-portal.md).

```sh
python -m enterprise_ai_lab.admin verify-report \
  --report reports/admin-recovery-v1/demo.json --identity-dir ../identity-boundary-lab
```

Verification checks recorded consistency and matching source bytes; it does not
rerun processes or prove execution. New actual runs require fresh paths. These
remain two fictional settings on a trusted loopback host: no system operations,
IAM change, external identity, model/provider call, production deployment, global
exactly-once delivery, externally anchored audit or registry release.
