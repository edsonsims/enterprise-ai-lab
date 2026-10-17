# Retrieval evaluation data contract

All inputs are UTF-8 JSON Lines: one object per line. Blank records, duplicate JSON
fields, extra fields, duplicate IDs, invalid types, and nonfinite scores are errors.
IDs and text fields must be nonempty strings. The CLI exits with status 2 for an
input or filesystem error.

## Documents

```json
{"id":"alder-payroll","tenant_id":"alder","allowed_roles":["finance"],"text":"Payroll corrections close on Friday."}
```

Required fields are `id`, `tenant_id`, `allowed_roles`, and `text`. Roles are a
nonempty list of unique strings. A document is authorized only if its tenant
matches the query identity **and** at least one role overlaps. An empty role list
on a query grants nothing. These are policy fixtures, not verified identities.
Missing or malformed identity/ACL attributes deny access in the ranker's policy
check; malformed datasets are rejected before evaluation.

## Cases

```json
{"id":"query-1","tenant_id":"alder","roles":["finance"],"query":"payroll corrections","relevant_doc_ids":["alder-payroll"],"category":"positive"}
```

Required fields are `id`, `tenant_id`, `roles`, `query`, and `relevant_doc_ids`.
`category` is an optional nonempty label. The two ID/role lists contain unique
strings and may be empty. Every relevance ID must exist and be authorized for
that case. An empty relevance list means no document in this corpus supplies an
authorized answer. It does not mean no relevant answer exists anywhere.

## Predictions

```json
{"case_id":"query-1","retrieved_doc_ids":["alder-payroll"],"refused":false,"scores":[3]}
```

Required fields are `case_id`, `retrieved_doc_ids`, and `refused`. `scores` is
optional; when provided, it must contain one finite number per retrieved ID.
Booleans are not numbers. Every case must have exactly one prediction and every
retrieved ID must exist in the corpus. Returned IDs must be unique; unauthorized
IDs are permitted in the input so leakage can be measured.

`refused` is an observed or predicted abstention flag. It is separate from
retrieval; a system may retrieve context and still refuse to answer. For the demo,
refusal is the simple heuristic "no ranked result." This heuristic has known
failures and is not an answerability classifier.

## Metrics

| Metric | Numerator or aggregation | Denominator |
| --- | --- | --- |
| Retrieval hit rate, answerable | Cases returning at least one relevant ID | Cases with a nonempty authorized relevance list |
| Macro recall, answerable | Mean of each case's fraction of relevant IDs returned | Same answerable cases; each case has equal weight |
| Query leakage rate | Cases returning at least one unauthorized ID | All cases |
| Returned-reference leakage rate | Unauthorized returned IDs, counted once per case | All returned IDs, counted once per case |
| Refusal accuracy, unanswerable | `refused=true` when relevance is empty | Cases with an empty authorized relevance list |
| Abstention classification accuracy | `refused` equals the emptiness of the relevance list | All cases |

A zero denominator yields JSON `null`, not a perfect score. Empty retrieval counts
as no returned-document leakage and as a retrieval miss for an answerable case.
The raw records retain both the prediction and unauthorized IDs so aggregate rates
can be checked. Any Wilson values in reports are descriptive arithmetic only;
the correlated synthetic templates do not support population confidence claims.

## Output and provenance

`report.json` stores metric definitions, experiment parameters, and limitations.
`results.jsonl` stores one joined case/prediction record per case. `documents.jsonl`,
`cases.jsonl`, and `predictions.jsonl` preserve the evaluated records.
`manifest.json` contains SHA-256 hashes for those five files. It detects content
changes; it is not a digital signature or proof that the input labels are true.

The same seed, implementation, and arguments produce the same demo files. A new
seed changes reference anchors and case order; it does not produce independent
customer observations. External data provenance remains the caller's responsibility.
