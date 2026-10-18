# Actual connected RAG observations

All inputs here are authored synthetic notes. The identity issuer, HTTP API,
JWT verification, MiniLM ONNX embeddings and persisted Qdrant searches are actual
implementations. No credentials, bearer tokens, private keys or model weights are
included. Human review and owner study have not been performed/reported.

- `extractive.json`: the strict 12-case connected CLI demo, including access
  refusals, scoped abstention and a Python-process restart without re-seeding.
  Exact cited content/answers persist; low-order cosine bits use a 1e-6 tolerance.
- `qwen-demo.json`: the documented optional CLI with actual local CPU decoding.
  Four generation-bearing requests returned structurally accepted evidence;
  empty-context requests skipped the model. The report preserves all responses.
- `qwen-strict-observations.json`: a separate actual HTTP probe with different
  question wording. Two of four actual generation calls returned Markdown-fenced
  JSON and were rejected with503. Two passed the literal evidence contract.
  It retains exact authorized contexts, rendered prompts, raw generated outputs,
  token counts, source/model hashes and all failures. One accepted why-question
  answer restates a value without explaining it.

The two neural records use different prompts. They must not be pooled into an
answer-quality score or treated as a held-out evaluation. Exact quotes/JSON
acceptance do not establish entailment, usefulness or instruction resistance.
Changing wording can change format compliance even with greedy decoding. The
strict contract was not relaxed and no extractive fallback hid a generation error.

The embedding revision/assets are pinned in the [model lock](../../src/enterprise_ai_lab/rag/model-lock.json);
the optional official Qwen3-0.6B revision is
`c1899de289a04d12100db370d81485cdf75e47ca`. Model weights stay outside Git. The
extractive CLI and tests are reproduced in Linux CI after explicit model bootstrap;
CI does not download or run Qwen. Neural observations were run on macOS arm64,
Python3.13.1, CPU FP32, four threads; full metadata is in each record.

Reproduction commands and limits are in the [repository README](../../README.md) and
[design notes](../../docs/identity-rag.md). Choose fresh store/output paths for a new observation.
`manifest.json` hashes the original recorded files; cosine scores are not expected
to be byte-identical across platforms or reloads.
