# Study 01: measure the retrieval boundary

Date: October 17, 2026. Status: implemented and exercised by automated agents;
owner study and human review have not yet been recorded.

## Question and hypothesis

Can a tenant-and-role filter prevent unauthorized retrieval without losing the
authorized results present in a small controlled fixture? Does that also make the
system refuse questions it cannot answer?

The first hypothesis is structural: if only authorized documents reach the ranker,
the returned IDs should be authorized. The second is deliberately tested rather
than assumed: an authorized result can still be irrelevant to the question.

## Dataset and method

The fixture generator creates 50 policy documents across ten fictional tenants
and five topics, plus 100 distinct query strings. Text and relevance labels are
templated synthetic examples, not customer tickets or independent observations.

| Category | Queries | Purpose |
| --- | ---: | --- |
| Authorized, shared terms | 50 | Relevant documents compete with similar documents in other tenants |
| Role denied, exact anchor | 10 | Exact match to a document the role cannot read |
| Role denied, shared terms | 10 | Ordinary payroll phrasing also overlaps permitted but irrelevant documents |
| Cross-tenant denied | 20 | Exact match to another tenant's document |
| No match | 10 | No matching corpus terms, including Unicode tokens |

Both systems use the same lexical ranker, deterministic ID tie-breaking, and `k=3`.
The permission policy requires matching tenant and at least one overlapping role.
The unsafe comparison omits that policy. The refusal heuristic is simply whether
retrieval returned no result; no LLM is run.

## Reference run

```sh
python -m enterprise_ai_lab demo --output reports/reference --seed 42 --k 3
python -m unittest discover -s tests -v
```

| Measured property | ACL prefilter | Unsafe global comparison |
| --- | ---: | ---: |
| Queries returning unauthorized IDs | 0 / 100 | 90 / 100 |
| Answerable queries with a relevant result | 50 / 50 | 50 / 50 |
| Correct refusals on unanswerable queries | 40 / 50 | 10 / 50 |

The [filtered report](../reports/reference/report.json),
[unsafe report](../reports/reference/unsafe_global_baseline/report.json), and
[per-case results](../reports/reference/results.jsonl) contain the denominators and
raw evidence. Both bundles include SHA-256 manifests.

The 10 shared-term payroll questions are the useful counterexample. An employee
cannot read payroll, but the tenant name overlaps unrelated accessible documents.
The filtered ranker therefore returns permitted context and the heuristic fails to
refuse. Zero returned-document leakage and correct answerability are different
properties. Future answer generation must be evaluated separately.

## Engineering verification

Fourteen local tests cover tenant/role isolation, missing identity, invalid ACLs,
Unicode, deterministic ranking, prefilter versus top-k postfilter behavior,
multiple relevant documents, metric denominators, malformed/missing/duplicate
records, nonfinite scores, deeply nested JSON, CLI behavior, and output hashes.
The final suite was run on Python 3.10.6 and 3.13.1 on macOS.

An independent automated review reproduced the demo and verified byte-identical
bundles on Python 3.10.6 and 3.12.14. The GitHub workflow additionally targets
Linux with Python 3.10, 3.12, and 3.13; actual runs are visible in
[GitHub Actions](https://github.com/edsonsims/enterprise-ai-lab/actions).
These checks do not establish physical infrastructure or live model behavior.

## Limits and next experiment

The fixture mostly encodes expected policy behavior. Its scores establish that the
implementation matches these examples, not that it will generalize. Wilson values
in the JSON are descriptive binomial arithmetic; the template correlations and
within-query reference correlations preclude population confidence claims.

There is no identity verification, permission revocation flow, secure database,
answer generator, or learned retriever. No performance/ROI result or new research
method is claimed. The next substantive experiment should replace one fixture
boundary with a tested real integration and retain the same negative tests.

## Owner study exercise

1. Run the study and inspect one case from every category. Recalculate one metric
   from the JSONL results without reading the report's aggregate.
2. Explain why filtering *after* top-k ranking can discard every candidate while a
   permitted relevant document still exists below the cutoff.
3. Change `k` to 1 and then 5. Predict which metrics will change, run both variants
   into `reports/local/`, and compare the observed results.
4. Design a query where all returned documents are authorized but none answer the
   question. Propose an abstention rule and explain its false-positive tradeoff.
5. Write a short reflection: what a successful ACL test proves, what it leaves
   unknown, and which integration should be tested next.

Do not claim these exercises completed until they have actually been performed.
