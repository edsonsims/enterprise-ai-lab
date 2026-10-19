# Synthetic legacy work orders, version 1

Authored for this repository on 2026-10-19. All work orders, assets, diagnostics,
tenants and role grants are fictional local study data under the repository's
MIT license. No customer, employer, actual equipment or public operational
dataset is represented. There is no Oracle/SAP connector or model call.

Alpha and Beta deliberately reuse all nine work-order IDs and three asset IDs.
Their text contains different visible sentinels so tests can detect a tenant
mix-up. IDs 2, 4 and 7 require the analyst role; the other six work orders allow
reader. These interleaved grants test that filtering happens in SQL before page
limits. Each tenant has three analyst-only diagnostic records.

The trusted local launcher supplies a fixed principal profile. This authored
policy is not login, authentication or synchronized enterprise entitlement.
The JSON is the source; explicit initialization creates a fresh SQLite database
and records its source SHA256. Serving neither seeds nor downloads data. The
source hash identifies the fixture JSON, not proof against a host administrator
altering the database. The host, schema and local filesystem remain trusted.
