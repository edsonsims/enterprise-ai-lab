# Local configuration and recovery desk

This study joins a separate real browser identity issuer to two bounded view
settings, a permission-filtered work-order source and durable configuration
receipts. All work orders are the existing authored fictional gateway fixture.
It changes no company system, process, model, identity policy or RAG corpus.

## Run the experiment

Use Python3.12+ and Node24. Install the existing exact HTTP/JWT environment;
the existing dependency locks remain unchanged. No model or provider is used.

```sh
python3.12 -m venv .venv-admin
.venv-admin/bin/python -m pip install -r requirements-approval.lock
.venv-admin/bin/python -m pip install --no-deps -e .
(cd ../identity-boundary-lab && npm ci --ignore-scripts && npm run build)
npm ci --ignore-scripts
npx playwright install chromium

.venv-admin/bin/python -m enterprise_ai_lab.admin demo \
  --identity-dir ../identity-boundary-lab --node node \
  --work-dir .local/admin-demo --out reports/local/admin-demo.json \
  --browser-test --browser-script tests/admin/browser.mjs

.venv-admin/bin/python -m enterprise_ai_lab.admin verify-report \
  --report reports/local/admin-demo.json --identity-dir ../identity-boundary-lab
```

The demo requires fresh work and output paths. Dependency/browser installation
is explicit setup; the experiment itself uses only local assets and loopback
HTTP. Generated account passwords remain in an owned0600 private file, never
in public reports, application logs or browser storage. Do not use personal
credentials. The [companion identity guide](https://github.com/edsonsims/identity-boundary-lab/blob/main/docs/browser-admin.md)
documents the fixed issuer, profiles and standalone bridge.

The installed wheel can run the HTTP/process experiment outside this checkout.
The optional Chromium driver is an explicitly supplied test script; it is not
silently located through the current working directory or bundled as runtime UI.
The actual HTML/CSS/JS application assets are in the wheel.

## Identity and allowed work

The issuer runs actual Authorization Code + PKCE S256, with a dedicated
`admin-portal` client and `urn:identity-boundary:admin` audience. The portal
independently verifies RS256 signatures, issuer, audience, user identity, client,
subject, expiry, roles and granted scopes. It also binds the ID token's nonce
and subject to the login. Review, RAG, approval and service tokens do not establish
an admin user. The issuer keeps its signing keys while the resource restarts.

| Operation | Required signed role | Required scope |
| --- | --- | --- |
| Read config and filtered work orders | config_admin or config_auditor | admin:read |
| Change config or create a rollback revision | config_admin | admin:configure |
| Read this tenant's committed mutation audit | config_admin or config_auditor | admin:audit |

Only `work_order_page_size` (integer1–10) and `default_status`
(`open`, `in_progress`, `closed`) can change. The default is page size3 and open
status. The real read-only SQLite query applies the signed tenant AND a permitted
row role before ordering and limiting. Administrative configuration permission
does not grant the analyst role or bypass row permissions. No browser input
can choose a tenant, actor, role, SQL statement, file path or system command.

The browser stores an opaque HttpOnly SameSite cookie; bearer tokens remain in
server memory. The authenticated page receives its session-bound CSRF value for
mutation headers; that value stays out of persistent browser storage and public
reports. Mutations require exact Origin and CSRF checks.
Sessions expire and disappear after portal restart. Login uses a separate
cookie-bound state/nonce/PKCE verifier; forwarded identity and proxy headers are
refused. The service listens only on a literal127.0.0.1 origin.

## Revisions, exact retries and rollback

`POST /api/commands` accepts exactly one of these complete bodies:

```json
{"command_id":"set-view-1","expected_revision":0,"action":"update","config":{"work_order_page_size":1,"default_status":"closed"}}
```

```json
{"command_id":"restore-defaults","expected_revision":1,"action":"rollback","target_revision":0}
```

The tenant and subject come from the verified identity. A receipt binds that
identity, the complete command, the previous revision, the resulting config hash
and a global mutation-audit hash. Configuration revision, metadata audit, receipt
and audit tip commit in one local SQLite transaction. A stale expected revision
or changed subject/body under the same tenant-local command ID returns409.

An exact committed retry returns its original receipt before revision comparison;
it cannot reapply old settings over later changes. Authorization and expiry are
checked again for replay and immediately before commit. Rollback copies a prior
configuration into a new revision; it never deletes earlier changes or receipts.
Revision0 is the declared default, independently available to each tenant.

## Recovery runbook

1. After an uncertain response, inspect current durable state.
2. Sign in again if the portal restarted or the session expired.
3. Retry only the original command ID and unchanged body, explicitly.
4. Use a new reviewed rollback command if a committed configuration must change.

The UI separates review from confirmation and never retries automatically. It
can retain an immutable pending command with its account/tenant in the current
tab's session storage, including across login navigation. This contains no token,
password or CSRF value. It is untrusted input and the server validates it again.
A different signed account cannot silently adopt it.

The process experiment kills a disposable resource before commit and after commit
but before the HTTP receipt. A trusted private test bootstrap selects those two
bounded scheduling barriers; the normal API exposes no fault or kill operation.
The first interruption leaves no partial revision/audit/receipt. The second can
leave a committed change with a lost response; the exact retry discovers it.
This is one-database transaction/replay evidence, not global exactly-once delivery.

## Storage and evidence limits

The history has a finite1000-change capacity and a bounded16MiB file budget.
Read and write admission is nonblocking; SQLite waits at most its configured50ms
busy timeout. These are work controls, not a hard real-time scheduling guarantee.
Audit responses contain the latest20 changes for the signed tenant; the server
validates the complete bounded chain, including its atomic count/tip, first.

The configuration database and the source database's main/WAL/SHM/journal file
families must be distinct. Private directory/file modes, inode continuity and
no-follow creation checks reject observed replacements and aliases. Existing
database files are never manually opened/closed during guards; SQLite manages
its descriptors so another connection's POSIX lock is not accidentally dropped.
The filesystem and local operator remain trusted. These checks are not a hostile
filesystem sandbox, encryption, secure erasure or an externally anchored audit.
An administrator who rewrites both history and its local metadata is outside
that proof. Corruption is refused; there is no automatic audit reset or repair.

See [the current follow-up](../reports/admin-recovery-v1) and the unchanged
[original frozen run](../reports/admin-v1) for actual process, browser and runtime
observations. Automated fixture-account actions are not owner review, personal
study, external SSO, production administration or an accessibility certification.
No inference, OCR, customer outcome, new quality score or registry release is
part of this study. Prior reports and their source identities remain unchanged.

## Exercises

- Explain why a lost HTTP response differs from a rolled-back transaction.
- Compare an exact retry after a later revision with a new rollback command.
- Inspect why a configuration administrator still cannot see analyst-only rows.
- Identify what the tenant-filtered audit projection proves and what needs an
  external anchor or independent retained receipt.
- Run the demo yourself, inspect a command and its receipt, and record your own
  observations separately from the automated report.

## Initial view and failure follow-up

The desk stays hidden until its initial configuration, filtered work-order page
and tenant audit are loaded. The rendered regression delays a real successful
configuration response, then releases exactly the original bytes; it uses no fake
API results. After a confirmed mutation, the browser checks wait for view-refresh
completion before the next inspection. Mutation IDs, authorization and retry rules
are unchanged.

A failed initial remote browser run did not retain its phase, so its cause remains
unknown. The finite diagnostic reports only a fixed browser failure code,
whitelisted stage, bounded return code, three initial-view HTTP statuses and an
allowlisted API error code from an owned0600/no-follow/single-link
regular JSON file of at most32KiB. Fixed stages distinguish the administrator,
auditor and second-tenant login. Request generations keep an earlier login's
response out of the next login's context. An unknown or absent status remains0;
these observations do not infer why a request failed. The diagnostic exception never includes raw exception text or driver
stdout/stderr. There is no new raw-log or private-directory artifact upload. The
[follow-up evidence](../reports/admin-recovery-v1) preserves both that limitation
and the separately reproduced readiness gap. Use the matching source checkout
when recomputing an older frozen report.
