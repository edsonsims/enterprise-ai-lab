# Fresh permission-aware knowledge case

On **2026-10-29**, a fresh private work directory and newly created Python 3.13.1
environment ran the connected publication workflow from an installed **0.14.1
wheel outside the source checkout**. This advances the first representative
case of roadmap item 15. Fresh reproductions of the other two cases and the
complete three-case milestone remain pending.

The actual run completed once in **55.834 seconds** and recorded **48 HTTP
observations, three committed publication effects and six SDK-replayed raw
Temporal histories**. The real Qdrant count was **0 before approval and 28 after
publication**. There was no retry of the demo after failure and no runtime
asset download. Installation of the explicitly locked dependencies was a
separate setup step.

The installed wheel's 113 package files match the clean source tree at
[`5768b4498bfbe83d26af69d1a5877b425819c19f`](https://github.com/edsonsims/enterprise-ai-lab/tree/5768b4498bfbe83d26af69d1a5877b425819c19f).
The wheel hash, individual package hashes, three unchanged runtime-lock hashes,
runtime versions, MiniLM/Temporal pins and exact Identity Boundary Lab revision
are retained in [provenance.json](provenance.json),
[wheel-source-inventory.json](wheel-source-inventory.json) and
[demo.json](demo.json). A fresh build of Identity Boundary Lab after the run
reproduced the same bridge bytes. The current identity revision differs from
the original 2026-10-18 report; this is a new current-runtime observation, not a
byte-identical reproduction of that historical environment.

| Boundary exercised | Observed result |
| --- | --- |
| Staged, rejected, expired and pending candidates | No searchable citations; zero indexed points before approval |
| Forged, wrong-audience, wrong-role, cross-tenant and missing-scope decisions | Expected 401/403 refusals; altered snapshot/revision conflicts remain visible |
| Temporal/resource/edge restart while pending | Exact immutable request and pending state preserved |
| Resource killed after vectors and ledger commit, before activity acknowledgment | Original raw history records actual attempt 2 after a StartToClose timeout; one primary effect |
| Native/OCR retrieval and tenant isolation | Cited authorized evidence; reader/analyst and Alpha/Beta boundaries preserved |
| Approved ACL narrowing, old-decision replay and stale approval | Reader visibility removed; analyst access retained; old permissions not restored |
| Resource restart and six raw SDK history replays | OCR citations preserved; no additional publication effects |

## Verify the recorded contract offline

The standalone verifier uses only Python's standard library:

```sh
python scripts/verify_knowledge_case.py --report reports/case-knowledge-v1/demo.json
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests/case_studies -v
```

It checks all 48 unique case IDs and statuses, the fixed 19 query cases and their
questions/identities, finite denial response envelopes and reading roles,
candidate/citation provenance, immutable request replay, publication revisions,
independent fixture actors and ACL results. It reads six fixed history files,
checks their hashes/event lists, decodes workflow inputs/results, binds effects
to their exact tenant/request, and checks the primary activity's recorded
attempt-2 timeout evidence. Missing, duplicate or failing expected cases cannot
silently drop out. Forty-seven new contract tests include negative permissions,
re-sealed history substitution, missing artifacts and private-content canaries;
the 21 historical-auditor tests remain unchanged. All **68 tests** passed locally.

This verifier checks consistency of recorded artifacts. It starts no service,
performs no SDK replay, repeats no extraction and proves no new execution. CI's
recorded-artifact checks do not run a model or establish human review.

## Reproduce the actual workflow

Use a separate clean checkout of the runtime source revision above, the Identity
revision in the provenance file, Node 24, Tesseract 5 English and the documented
hash-pinned MiniLM and Temporal assets. Preserve existing checkouts and use new
private work/report paths. Follow the [publication setup](../../docs/approved-publication.md)
for all three exact dependency locks. Build a wheel from that source, install it
with `--no-deps` into a new environment, then run from outside the checkout:

```sh
"$CASE_PYTHON" -I -m enterprise_ai_lab.publication demo \
  --temporal-cli "$TEMPORAL_CLI" --identity-dir "$IDENTITY_DIR" --node "$NODE_BINARY" \
  --model-dir "$MODEL_DIR" --manifest "$LAB/public-data/pdf-v1/ingestion.json" \
  --work-dir "$NEW_PRIVATE_WORK" --out "$NEW_PRIVATE_REPORT"
python "$LAB/scripts/verify_knowledge_case.py" --report "$NEW_PRIVATE_REPORT"
```

The shell names represent explicit local paths, not configured credentials.
Wheel archive timestamps or metadata can change its hash; compare the recorded
package-file inventory rather than assuming a newly built archive has the same
bytes. Serving does not bootstrap or download models. A fresh run writes fresh
evidence; it must not replace the historical report or this versioned run.

## Public-history derivative and limits

The failed stale-publication history contained private runtime paths in four
`stackTrace` fields. Its public derivative replaces only those values with
`[private runtime stack omitted]`. The public demo updates that history hash and
adds an explicit artifact-scope note; all observations, effects, candidates and
runtime fields are unchanged. The other five histories retain their original
bytes. Provenance retains both raw-private and public hashes and the exact four
redaction paths. The **actual SDK replay used the original private raw histories**;
no SDK replay of the public derivative is claimed. Raw private issuer IPC,
credentials, databases, work paths and logs are excluded.

The issuer remained running during the recovery exercises; issuer persistence
and key rotation are not tested. The reviewer is a scripted OAuth service
identity, not a logged-in person or owner review/study. The public inputs remain
two selected English NIST pages and one labelled clean OCR derivative with their
[original rights and attribution](../../public-data/pdf-v1/README.md).
Literal evidence is not semantic entailment. This is no OCR/answer-quality
benchmark, customer result, production deployment, global exactly-once guarantee
or administrator-proof authorization boundary. Partial vector writes still fail
closed and require the documented fresh-store recovery; this successful
after-commit interruption does not replace those separate controlled tests.
