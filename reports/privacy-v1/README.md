# Scoped local privacy experiment — 2026-10-20

Two distinct observations are preserved here:

- [`demo.json`](demo.json): actual local OAuth issuer, Python resource processes,
  private SQLite maps and a deterministic loopback HTTP processor. It records
  **25 HTTP observations**, three resource processes, three downstream requests,
  **14 metadata-only audit rows** in the primary vault, restart restoration,
  a real SQLite lock and a real one-second context expiry. All three resource
  processes were reaped. The issuer stayed running during resource restart.
- [`evaluation.json`](evaluation.json): all **36 fictional development cases**,
  original gold spans, detector predictions, per-case false positives/misses,
  exact-span counts, formulae, source hashes and runtime metadata. This is
  separate from the process demo and its three supported substrings.

The macOS arm64 run uses Python3.13.1, Node24.19.0, httpx0.28.1 and PyJWT2.15.1.
The demo's actual locked-database response took **0.074118375 seconds** on this
machine; this single observation is not a performance distribution or guarantee.
The resource log scan examined **156 bytes**, finding none of the demo's known
supported values, complete source string or minted bearer credentials. Raw
private issuer IPC was excluded. This finite scan is not a universal no-leak proof.

The downstream tool received placeholders instead of the supported email/phone/
card-like substrings, and received no Authorization header. It deliberately
received the fictional name **Mara Vale**, which V1 does not detect. An injected
unknown placeholder invalidated the whole restoration. A failed downstream call
left its already committed map subject to ordinary expiry/capacity bounds.

| Exact-span metric denominator | Gold | Predicted | TP | FP | FN | Precision | Recall | F1 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Supported kinds | 29 | 29 | 28 | 1 | 1 | 0.965517 | 0.965517 | 0.965517 |
| All annotations | 40 | 29 | 28 | 1 | 12 | 0.965517 | 0.700000 | 0.811594 |

The quoted-mailbox miss and public-contact false positive remain in the report.
The eleven annotations outside V1's emitted vocabulary have zero exact matches
by construction; that declared limitation is retained in the wider denominator.
No detector/corpus adjustment followed scoring. There is no held-out,
real-person, general privacy-accuracy or neural-model comparison claim.

## Reproduce and interpret

Follow the [complete commands and protocol](../../docs/privacy-proxy.md). A new
run needs a fresh private work directory **and output path**. The evaluation can
be checked in the matching checkout with:

```sh
python -m enterprise_ai_lab.privacy_proxy verify-report reports/privacy-v1/evaluation.json
```

Verification checks the fixed corpus, source hashes, every case/prediction and
all recomputed metrics. It does not authenticate the self-recorded timestamp or
platform strings. The process report fingerprints the runtime Python modules
and the built Identity issuer/bridge. It was captured from the milestone working
tree; hashes identify the executed bytes independently of a later commit ID.
Preserve these files when implementing future versions.

SHA-256 of the original report bytes:

| File | SHA-256 |
| --- | --- |
| `demo.json` | `9ea319b83385036ca8d452f5c7d163867293db0f1b5b61eadca4d5b5ab1a34b3` |
| `evaluation.json` | `e523f024c24811729ef45943709d72d83efd0e495eaa7e2781c0477aad3e0d8e` |

The local token map is plaintext. Expiry is logical deletion, not encryption,
secure erasure or a retention guarantee. The metadata audit is finite, local and
unanchored. There is no transaction across SQLite and the processor, automatic
redaction retry, external provider, production deployment or compliance result.
The original lexical/RAG/PDF/approval/publication/review/gateway reports are unchanged.

## Verification alongside the existing studies

The combined local Python3.13.1 suite passed **691 tests / 408 subtests**,
including **133 privacy tests / 96 subtests**. It enabled the actual RAG,
PDF/OCR, durable approval/publication, browser review/Chromium, controlled
diagnostic, official MCP and new privacy process integrations. The original
dependency-free 14 tests also passed and reproduced the original reference
reports byte-for-byte. Identity Boundary Lab's dedicated extension passed
111 Node24 tests locally and the actual Node22/24 CI jobs.

A separately built 0.8.0 wheel was installed with the exact approval lock into
a fresh Python3.12.14 environment. From an unrelated directory with `PYTHONPATH`
unset, imports resolved to installed site-packages, the bundled 36-case corpus
matched its hash, evaluation/recomputation passed and the actual 25-case process
demo completed. `pip check` passed; all three resource processes were reaped.
No package registry release is represented.

These checks cover local synthetic behavior and bounded failures. They do not
establish production security, generalized detection or real-data retention.
