# One local CPU inference run

Recorded 2026-10-25, 12:24:29–12:24:59 UTC on an Apple M4 Max with 64 GiB RAM,
macOS 26.6.2 / arm64, Python 3.13.1. This uses the existing hash-pinned Qwen3-0.6B
checkpoint and exact Torch 2.14.1 / Transformers 5.20.1 runtime. The 0.13.0 wheel was
built locally and installed into a private target outside the checkout; module
locations and the six runtime source hashes matched before inference.

| Configured PyTorch intraop threads | Worker startup (s) | Parent to ready (s) | First request (s) | Warm median, 6 requests (s) | Process peak RSS (bytes) |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 5.980 | 6.016 | 1.381 | 1.502 | 4,980,621,312 |
| 4 | 2.216 | 2.249 | 1.372 | 1.494 | 4,985,864,192 |

These are descriptive observations from one fixed-order run. Startup includes
file-hash verification and model loading; the parent additionally includes spawn
and module import. Later filesystem-cache/thermal/background effects were not
controlled. The startup difference does not isolate a thread-count effect.
Peak RSS was already reached by `ready` and did not increase in the request
receipts. It is whole-process lifetime highwater, not weights-only or incremental
request memory. The recorded Darwin native unit is bytes.

All 14 planned requests ran: one first request and six warm requests for each
configuration. Both actual `model.generate` counters ended at 7. The retained
traces contain 864 generated tokens in total, including the two first requests;
input counts are 183–186 tokens. All 14 outputs passed the literal chunk/quote and
JSON checks. That observation is not an answer-quality or entailment score. No
missing token trace or terminal error occurred. Both owned workers exited 0 and
were reaped without forced termination; cleanup took 0.259 s / 0.268 s. Each worker's
stderr was drained into count/hash metadata, without publishing its raw bytes.

## Files and reproduction

- [`cpu-qwen-v1.json`](cpu-qwen-v1.json): unchanged raw measurement report, including
  first/warm timings, actual tensor counts, all outputs, memory observations and
  exact checkpoint/runtime/source fingerprints.
- [`execution.json`](execution.json): allowlisted installed-artifact/host and
  macOS network-denial observations, with no private paths or credentials.
- [`manifest.json`](manifest.json): hashes of those two JSON artifacts.
- [Fixed fictional requests](../../src/enterprise_ai_lab/edge/data/README.md) and
  [measurement contract](../../docs/edge-inference.md).

```sh
# Model-free validation and recomputation:
python -m enterprise_ai_lab.edge verify-report \
  --report reports/edge-v1/cpu-qwen-v1.json
python -W error::ResourceWarning -m unittest discover -s tests/edge -v
# Explicit actual inference, after the optional exact runtime/assets setup:
.venv-generator/bin/python -m enterprise_ai_lab.edge demo \
  --model-dir /path/to/pinned/snapshot --out reports/local/edge-new.json
# The recorded experiment additionally ran under this macOS process policy:
/usr/bin/sandbox-exec -p '(version 1)(allow default)(deny network*)' \
  .venv-generator/bin/python -m enterprise_ai_lab.edge demo \
  --model-dir /path/to/pinned/snapshot --out reports/local/edge-denied-new.json
```

The control immediately before inference received EPERM for both TCP and UDP
attempts to owned loopback listeners, with no observed delivery. The inference
process and its workers inherited the same OS policy. This proves this bounded
process experiment, without claiming physical disconnection, a packet-capture
audit, Linux inference or a deployable optional-runtime/model bundle. No model
or dependency was downloaded and the existing neural environment was unchanged.

Local validation comprises 51 unique applicable methods: 17 edge contracts/process
cases, 20 existing bundle cases and 14 original lexical cases. The same 17 edge
methods also passed on Python 3.10.16; that repeat adds no unique cases. Controlled
workers/generators in tests are explicit doubles, separate from the 14 actual
neural calls above. CI revalidates recorded metrics on Python 3.10/3.12/3.13 and
exercises actual controlled pipes/processes, without running the model.

No p95/p99, capacity, power, quality, GPU/NVIDIA, hardware-superiority, customer or
production claim follows. The fixture was frozen before inference and contains
no expected-answer fields. Earlier Qwen/RAG/other study reports remain unchanged.
