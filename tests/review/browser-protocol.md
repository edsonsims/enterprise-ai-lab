# Actual browser review protocol

`browser.mjs` drives real Chromium through the local review UI and the real
Identity Boundary Lab account login. It does not replace HTTP routes, return
mock decisions, inject a session or write directly to the document index.

The parent integration harness must run the real local issuer, Temporal server,
publication worker and review portal. Before starting the browser, it submits
three pending review requests through actual requester account logins:

| Default request | Tenant | Candidate | Browser action |
| --- | --- | --- | --- |
| `browser-main` | Alpha | Native NIST Zero Trust page 15 for `reader`; OCR NIST AI RMF original page 25 for `analyst` | Reader reviewer is restricted; analyst reviewer approves |
| `browser-reject` | Alpha | Native NIST Zero Trust page 15 for `reader` | Analyst reviewer rejects |
| `browser-beta` | Beta | Public pages with the Beta reader grant | Beta reviewer approves |

The fixture account file has the same private schema consumed by the issuer:

```json
{"schema_version":1,"accounts":[{"username":"reviewer-alpha","password":"generated privately by the demo"}]}
```

That illustration is not a usable account file. The actual file must be owned
by the current process user, mode `0600`, at most 32 KiB, and include generated
credentials for `reviewer-alpha`, `analyst-reviewer-alpha` and `reviewer-beta`.
Passwords are passed only through this private file and the real login form.
Do not publish the file or use a real person's password.

With the development-only locked Playwright package and its explicitly installed
Chromium available, run:

```sh
node tests/review/browser.mjs \
  http://127.0.0.1:PORT \
  /private/path/generated-accounts.json \
  reports/local/review-browser/browser.json
```

The environment variables `REVIEW_BROWSER_MAIN`, `REVIEW_BROWSER_REJECT` and
`REVIEW_BROWSER_BETA` can change the three synthetic request IDs. A fresh run
needs pending requests and a fresh local store: the browser really approves or
rejects these requests, so a completed run is not a resettable visual fixture.

The assertions cover:

- Actual Authorization Code + PKCE login and opaque HttpOnly browser session.
- No tokens in browser storage, JavaScript cookies or the session JSON.
- Reader-only review of the native page; hidden OCR page and a disabled decision
  for a partially readable batch; forged OCR PDF request refused with 404.
- Actual PDFium-rendered native and OCR page pixels, decoded as visible images
  by the browser, with returned PNG signatures/dimensions matching the decoded
  image. The separate Open PDF link returns permitted single-page PDF bytes.
  Tests also cover the PDF page-count headers, source page mapping, canonical
  text/chunks and the exact immutable approval digest.
- A mutation without the session CSRF token refused with 403.
- Actual UI approval, a durable receipt, and a separately observed completed
  publication. The browser waits for the real publication worker.
- UI rejection, an accepted decision audit event and a distinct publication
  observation in the tenant-scoped audit view.
- Beta queue isolation, including a direct Alpha detail request refused with 404.
- Logout clearing protected text/page images and invalidating the previous server
  session cookie; a 390-pixel layout check; no uncaught browser errors.

The output contains only safe assertion outcomes, the actual Chromium version,
true recording time and two screenshots of authenticated synthetic review
screens. It excludes credentials, cookies, CSRF values, OAuth codes, tokens,
browser traces, HARs and private local paths. Failed runs emit a deliberately
generic safe partial report. Diagnose failures locally without publishing
unfiltered browser logs.

Python integration tests independently parse preview PDFs with `pypdf` and check
that they contain exactly the selected page. This browser script checks the
actual rendered review controls, visible decoded PNG pixels, and separately
returned PDF bytes/headers. It does not depend on a browser's native PDF plugin;
a PDF response header alone is not an independent page-count measurement.

These are **automated browser actions using fictional local accounts**. They do
not establish that the owner studied or approved a document, that anyone read
the content, or that production authentication is implemented. The real manual
interface is available for a person to perform those actions separately.
