# A local account, the source page and an explicit decision

The verified local process run records **37 HTTP observations, four single-page
PDF previews, four publication effects, six exported/replayed Temporal histories
and 85 local audit events**. The actual Chromium pass records **11 successful
browser checks**, using Chromium 151.0.7922.34 and Playwright 1.62.1. Automated
fixture accounts performed every action; `human_review_performed` remains false.
Earlier reports remain unchanged.

[Full process report](demo.json) · [Chromium checks](browser.json) ·
[Desktop screenshot](browser-review.png) · [390-pixel screenshot](browser-review-mobile.png)

Combined local verification on Python 3.13.1/macOS arm64 passed **439 tests
and 247 subtests**, with every actual integration and the Chromium path enabled.
The 85 review tests include 75 focused boundaries and ten real process cases. The audit row count can vary with page views and polling
timing; the recorded 85 rows are not an expected fixed benchmark.

The implementation connects a separate real local OIDC Authorization Code +
PKCE issuer to the existing durable document-publication workflow. The portal
keeps user tokens in server memory, checks tenant and document roles, shows
individually permitted PDFium PNG page images beside exact text/chunk provenance,
provides a separately authorized single-page PDF link, and accepts independent
approval or rejection. The workflow receipt and eventual vector
publication are separate states.

The actual harness exercised:

| Boundary | Observation retained by the harness |
| --- | --- |
| Actual fixture-account form login and PKCE exchange | Distinct user subject, issuer-assigned tenant and roles; no service identity relabelled as a person |
| Reader reviewer opens a mixed reader/analyst batch | Only the native page is visible; OCR PDF request refused; complete-batch decision denied |
| Analyst reviewer opens the same batch | Native and actual OCR text, exact chunks, decoded PDFium PNG pages and individually permitted single-page PDF links |
| Missing CSRF, wrong Origin, other tenant or observer | Rejected access without document disclosure |
| Pending review and subsequent accepted decision | No pending RAG context; approved content becomes queryable through the existing permission filter |
| Rejection and identical decision retry | Rejected candidate remains outside search; matching retry retains its accepted receipt |
| Portal restart and logout | Ephemeral session denied; re-login retains durable workflow state and local audit |
| Exported Temporal histories | SDK replay does not create publication effects |
| Actual Chromium pass | Rendered controls, decoded PNG pixels/dimensions and PDF bytes/headers, approval/rejection clicks, audit, isolation, sign-out and narrow viewport |

Reproduce with all three optional runtime locks, Python 3.12+, Node 24,
Tesseract 5 English, the existing pinned MiniLM assets and Temporal CLI, and
Identity Boundary Lab commit `70e663c69ad96997c394581fa9df33487d2e3390`.
That issuer's [Node 22/24 CI](https://github.com/edsonsims/identity-boundary-lab/actions/runs/39139597620)
passed its 100 tests and original service demo:


```sh
.venv-review/bin/python -m enterprise_ai_lab.review demo \
  --temporal-cli .local/temporal-bin/temporal \
  --identity-dir ../identity-boundary-lab --node node \
  --model-dir .local/models/minilm \
  --manifest public-data/pdf-v1/ingestion.json \
  --work-dir .local/review-demo \
  --out reports/local/review-demo.json
```

For actual Chromium, first run `npm ci --ignore-scripts` and explicitly install
its browser with `npx playwright install chromium`, then add `--browser-test`
and use a fresh work directory. The full [setup and study guide](../../docs/human-review.md)
explains `--stay-open` for manual use and the
[browser protocol](../../tests/review/browser-protocol.md) distinguishes rendered
browser checks from HTTP cookie/redirect/form checks. Python separately parses
preview PDFs to verify that only the authorized page is present.

Private generated fixture passwords, cookies, CSRF values, authorization codes,
bearer tokens, raw browser traces and local databases must not be published.
The public report may retain safe response bodies, page-preview hashes,
source provenance, local audit events, runtime fingerprints and sanitized
browser assertions/screenshots. The frozen report includes the actual
Python/Node, PDF/OCR, Temporal, ONNX and Qdrant runtime versions, pinned executable
and model fingerprints, and SHA-256 hashes of both built `review-bridge.js` and
`review-issuer.js`. Candidate and audit stores remain local.

The source publications and prior raster fixture keep their
[attribution and derivative notices](../../public-data/pdf-v1/README.md). Review
PDF previews are separately labelled one-page derivatives with hashes distinct
from the original and processed source PDFs. The on-page viewer displays a
PDFium-rendered PNG with its own render hash; the browser verifies actual decoded
pixels and dimensions. Page-level access does not grant a whole-file download.

These are **automated fixture-account actions**, not proof that the owner or
another person reviewed or approved a document. The manual interface is
available for that separate activity. The local audit chain detects ordinary
retained-log corruption but is not externally anchored or tamper-proof against
host administration. Intent/receipt reconciliation is not a transaction across
Temporal, SQLite and Qdrant. Loopback HTTP, trusted source files/manifests and
the development gRPC administration boundary remain explicit limitations.
There is no new OCR accuracy, neural answer-quality, production security or
customer-outcome claim.


## What rendered-browser verification caught

Direct HTTP form tests originally passed while real Chromium login failed. The
issuer's `Referrer-Policy: no-referrer` caused `Origin: null` on the form POST,
which its strict Origin check rejected. Using `same-origin` retains the browser's
real same-origin Origin and suppresses cross-origin referrers; null Origin is
still rejected. Chromium then exposed a second issue: self-only CSP `form-action`
blocked the valid authorization redirect back to the portal. The issuer now
permits only self and the one statically registered loopback portal origin.
Exact callback validation, PKCE, Origin and CSRF checks remain in force. Node
regressions assert both response policies and null-Origin refusal; the actual
Chromium run subsequently completes login and all 11 checks.
