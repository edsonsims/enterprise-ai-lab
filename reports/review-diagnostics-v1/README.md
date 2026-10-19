# A 503 can follow an accepted workflow start

This **controlled local experiment**, recorded on 2026-10-19 at 16:17:29 UTC,
deliberately omits the publication worker during the first signed-in request.
It is **not a reproduction or explanation** of the earlier intermittent Linux
review CI failure; that cause remains unknown.

| Observation | Actual result |
| --- | --- |
| Initial immutable POST | HTTP 503, public `unavailable`, 3.011631 seconds observed |
| Safe failure metadata | `review_create` → `temporal_query`, SDK `CANCELLED` |
| Independent Temporal inspection | One running execution/start, zero completed workflow tasks |
| After real worker completes a task | One identical explicit POST returns HTTP 200 / pending |
| Workflow identity | Same execution; one total start |
| Publication | Zero scheduled activities, zero effects, tenant revision 0 |
| Actual authenticated RAG | HTTP 200, abstained, zero citations |

[demo.json](demo.json) retains the measured timings, exact RPC enum, independently
read history event summaries, immutable submission fingerprint, public page
provenance, pinned asset/runtime versions and source hashes. [failure.json](failure.json)
is the separate finite metadata snapshot. Neither contains account passwords,
tokens, document bodies, private runtime paths or child stderr. Only these
reviewed public-fixture artifacts are published; the local work directory stays
private. The demo SHA-256 is
`d742ec40f71f64272e91a04026636588df13db434f431a5d3059c2fda5cb993f`.

The public input is the byte-pinned NIST native/OCR fixture already described in
[pdf-v1](../pdf-v1). Its existing tenant/role grants, original/artifact hashes and
physical pages are retained. The process uses actual local account login with
Authorization Code + PKCE, persistent development Temporal, and the existing
resource with MiniLM/Qdrant. Login is performed by the protocol test client;
the separate unchanged review demo exercises the rendered Chromium interface.
No approval or person review occurs in this controlled probe.

## Reproduce

Follow [the review setup](../../docs/human-review.md) with Python 3.12+, the exact
RAG/ingestion/approval locks, Node 24, the pinned Identity Boundary Lab checkout,
Tesseract 5 English, and explicitly bootstrapped MiniLM/Temporal assets. Runtime
execution makes no model download or paid call.

```sh
python -m enterprise_ai_lab.review.diagnostic_probe \
  --temporal-cli .local/temporal-bin/temporal \
  --identity-dir ../identity-boundary-lab --node node \
  --model-dir .local/models/minilm \
  --manifest public-data/pdf-v1/ingestion.json \
  --work-dir .local/review-diagnostic --out reports/local/review-diagnostic.json
```

Choose fresh paths. Seven focused tests, including three actual process checks,
passed with four subtests on Python 3.13.1/macOS arm64; enable
`RUN_REVIEW_DIAGNOSTIC=1` alongside the documented runtime variables. CI enables
the same controlled path on Linux/Python 3.12 and 3.13. A job is only verified
after its actual conclusion is read.

## Interpretation and provenance

The working-tree source hashes bind the code actually run. The recorded
repository commit `1f898c70bfa926bf9bdff24987b1cd67141735ee` is its **base commit**,
not a claim that the added diagnostic code already existed there. The report
explicitly marks this distinction. Selected hashes include the new diagnostic
code, instrumented API/gateway/core, launcher/integration and immutable workflow,
plus the already pinned public ingestion manifest. Source stability was checked
before saving; original reports were not regenerated.

`CANCELLED` is the status actually returned by Temporal SDK 1.34.0 with CLI 1.9.1;
it is not relabelled `DEADLINE_EXCEEDED`. The configured per-RPC limit stays three
seconds and the API operation bound twelve. Elapsed HTTP time and that enum do
not reveal the SDK/server's internal cancellation cause. Other local attempts
had different elapsed times; this is a frozen observation, not a latency benchmark.

The first response does not mean the workflow start was rejected. Recovery waits
for a real completed worker task, then repeats the exact request once; no new
IDs, replacement deadline, automatic retry or approval is introduced. It remains
pending and outside search. This validates one ambiguity/recovery scenario,
not global exactly-once execution or production readiness. Development gRPC,
host/filesystem and local account policy remain trusted. The safe diagnostic sink
is optional and best effort, separate from the durable audit.
