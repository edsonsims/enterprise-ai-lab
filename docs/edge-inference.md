# Measure a local inference node

The node accepts a bounded question and explicit context over stdio, runs the
existing pinned Qwen3-0.6B CPU adapter and returns an evidence-checked response or
a finite failure. A separate process owner compares two configurations using the
same authored inputs. This is a trusted local launcher, without a network endpoint,
user login, tenant policy or permission connector. Authorized retrieval remains
in the earlier RAG resource.

## Run the actual model

Use Python 3.12+ and the existing `requirements-generator.lock` runtime. Installing
dependencies and bootstrapping model assets are explicit connected setup steps;
serving and measurement never download them. The exact checkpoint, nine file
hashes and four runtime versions remain in `rag/neural.py`.

```sh
python3.13 -m venv .venv-generator
.venv-generator/bin/python -m pip install -r requirements-generator.lock
.venv-generator/bin/python -m pip install --no-deps -e .
# Explicit first-time bootstrap, if the pinned assets are not already available:
.venv-generator/bin/python -m enterprise_ai_lab.rag.neural bootstrap \
  --cache-dir .local/model-cache
# Use the snapshot directory printed by bootstrap (or an existing verified copy).
mkdir -p reports/local
.venv-generator/bin/python -m enterprise_ai_lab.edge demo \
  --model-dir /path/to/pinned/snapshot --out reports/local/edge.json
python -m enterprise_ai_lab.edge verify-report --report reports/local/edge.json
```

Use a new report path. It is created exclusively with mode 0600; existing output
is refused before any worker starts. A terminal startup/process failure retains
the partial report and returns exit 2. A model-output validation failure remains a
measured request, with its raw output and token trace when available. It does not
trigger a fallback, another call or a retry. `verify-report` can successfully
validate a failure-containing report; its `terminal_errors` field stays nonzero.

For a reusable single worker:

```sh
.venv-generator/bin/python -m enterprise_ai_lab.edge serve \
  --model-dir /path/to/pinned/snapshot --threads 4
```

The first stdout line is `ready`. Then send one JSON object per line, with exactly
`id`, `question` and `chunks` (`chunk_id`/`text` pairs). For example:

```json
{"id":"example","question":"When does the fictional window begin?","chunks":[{"chunk_id":"fictional:0","text":"The fictional maintenance window begins at 02:00 UTC."}]}
```

EOF closes the input and emits `stopped`. The worker accepts at most 32 requests,
1–8 unique chunks and a 24 KB serialized context cap. The adapter separately
refuses over 4096 input tokens and never truncates the prompt. No request can
change the checkpoint, thread count or generation settings. Model/library output
is kept off stdout so it remains a JSONL protocol; stderr bytes are drained and
only their count/hash enter the report. The raw model text is explicitly retained
in each available request trace. Keep reports private when using private context.

## What the measurements mean

The [frozen protocol](../src/enterprise_ai_lab/edge/data/README.md) schedules one
fresh process with 1 intraop thread, then another with 4. Each process hash-checks
all model files before loading float32 weights on CPU, sets the thread count
before execution, disables thinking/sampling and caps generated tokens at 128.
The first request after load is kept separately from six subsequent warm requests.
There are 14 planned calls; the report counts actual `model.generate` invocations,
including format failures and first requests. Missing token traces stay explicit.
If a worker dies before its receipt, unobserved work and tokens are unknown.

`parent_ready_elapsed_ns` includes process spawn, module import, asset verification
and model startup. Worker `startup_elapsed_ns` begins just before construction of
the adapter. Request time includes context/prompt formatting, tokenization,
generation, decoding and evidence validation. `model_generate_elapsed_ns` covers
only the actual model method. Nanosecond clocks express a duration's unit, not
measurement accuracy. The warm median is stored exactly as twice its value to
avoid fractional JSON numbers.

`ru_maxrss` is the worker process's lifetime high-water mark across all its
threads. On the recorded macOS host the native unit is bytes, as specified by its
local `getrusage(2)` manual; Linux uses KiB and is normalized by 1024. RSS does not
measure incremental request allocation, solely weights, child-process memory,
GPU memory or physical memory available. A later high-water value cannot decrease.
The 1/4 settings control PyTorch intraop parallelism, without promising an exact
number of OS threads, core affinity or use of all cores.

Every pipe read/write uses the parent's single 180-second absolute worker lifetime.
After work, a separate single 5-second cleanup budget allows graceful exit and
reserves time to kill/reap the owned worker process group. These are acceptance
and observed cleanup budgets, not hard real-time scheduling guarantees. The
adapter's 10-second generation setting is soft: a decoding pass can finish after
it. There is no cancellation rollback or continuation of a killed request.

## Reproduce the record without loading a model

```sh
python -W error::ResourceWarning -m unittest discover -s tests/edge -v
python -m enterprise_ai_lab.edge verify-report \
  --report reports/edge-v1/cpu-qwen-v1.json
```

These model-free checks validate the fixture, source/model/runtime fingerprints,
request order, token limits, literal evidence, repeated-prompt consistency,
monotonic RSS, outcomes, cleanup and recomputed counts/median. They run on the
core Python 3.10+ installation. Controlled generators/processes in tests are
explicit test doubles. CI verifies recorded results and real pipe/process
boundaries; it does not run neural inference or measure Linux model performance.
Source hashes bind a record to this implementation; they are not a signature or
independent attestation that an operator's report is truthful.

## Tradeoffs and limits

One model per fresh process is easy to stop and separates startup from warm work,
but repeats asset verification and model loading. Sequential runs avoid overlapping
workers while leaving fixed-order, OS-cache, thermal and background-load effects.
Three fictional requests and six warm observations per configuration do not support
p95/p99, throughput/capacity, power, quality, superiority or generalization claims.
The host is not an isolated benchmark machine, and filesystem caches were not
flushed. Literal quotation checks do not establish semantic entailment.

This CPU-only milestone does not implement a full edge fleet, HTTP service,
production isolation, GPU execution, NVIDIA vLLM/TensorRT/MIG, or the optional
runtime/model/container delivery pipeline. The prior core-wheel bundle contains
none of these assets. Existing RAG/PDF/Qwen reports remain distinct and unchanged.

## Study exercises

1. Reproduce the recorded summary without Torch. Explain why a failed generated
   JSON response remains in both the call count and the latency sample.
2. Trace the first request from process spawn through tokenization and generation.
   Explain why calling it a disk-cold benchmark would be unsupported.
3. In a new versioned protocol, change a context size or configuration order and
   retain every result. Compare effects without turning three cases into a quality
   benchmark or silently tuning the frozen v1 fixture.
4. Start a controlled worker that stalls or closes stdout. Show the difference
   between a finite model response failure and a terminal process failure, and
   verify the owned child has been reaped.

These are exercises available to the owner, not a record of personal study.

## Primary references

- [Pinned Qwen3 model card](https://huggingface.co/Qwen/Qwen3-0.6B/blob/c1899de289a04d12100db370d81485cdf75e47ca/README.md)
- [PyTorch 2.14 intraop thread setting](https://docs.pytorch.org/docs/2.14/generated/torch.set_num_threads.html)
- [Transformers generation settings](https://huggingface.co/docs/transformers/main_classes/text_generation)
- [Python 3.13 resource usage](https://docs.python.org/3.13/library/resource.html)
- [Linux `getrusage(2)` native KiB units](https://man7.org/linux/man-pages/man2/getrusage.2.html)
