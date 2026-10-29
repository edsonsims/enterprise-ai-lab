# Identity to approved, permission-filtered knowledge

The historical question was whether independently verified identity and durable
approval could constrain what entered retrieval and what a reader could see.
The [publication report](../../reports/publication-v1/README.md), recorded on
2026-10-18, connects the actual local OAuth/JWKS issuer, trusted native/OCR page
grants, immutable candidates, Temporal approval and MiniLM/Qdrant publication.
It records 48 HTTP observations, three publication effects and six replayed
histories, with 28 final chunks. Pending, rejected and expired candidates remain
outside search. An approved ACL revision removes reader visibility while keeping
analyst access. Repeating a committed effect does not reapply an obsolete ACL.

The important boundaries are separate: a valid identity is not permission for
every document; approval is not a completed publication; a timeout is not proof
that an effect failed. Tenant AND role filtering occurs before retrieval, and
partial vector mutations preserve the fail-closed fence rather than reopening
search for availability. The [source archive](https://github.com/edsonsims/enterprise-ai-lab/tree/6094d61dcf09e2a75005f246e89eba00fbaba8eb)
provides the original publication context. The byte audit checks the unchanged
report, without running those systems or asserting that execution used a clean
committed checkout.

This is a local study over two selected English public pages and one clean OCR
derivative. Service-account decisions are scripted fixtures, not human or owner
review. Literal evidence is not semantic entailment, and the experiment does not
measure broad OCR or answer quality. Production IAM, entitlement synchronization,
distributed vector serving and issuer persistence/rotation remain separate work.
The inventory audit itself does not repeat these experiments.

## Fresh representative run, 2026-10-29

A [new current-runtime run](../../reports/case-knowledge-v1) uses an installed
0.14.1 wheel outside its checkout, all three exact runtime locks and the current
dedicated issuer bridge. It records the actual 48 HTTP observations, three
publication effects and six raw SDK history replays, including restart while
pending, an after-commit worker interruption, tenant/role visibility and a
revision-checked ACL narrowing. Package files, executable/model hashes and source
revisions bind this run separately from the historical source above.

The standalone recorded-contract verifier checks case completeness, identities,
literal citation metadata, exact requests/effects and decoded history payloads.
It does not rerun services or prove a new execution. The public failed-history
derivative omits four private runtime stack traces; actual replay used the raw
private histories, as declared in the new provenance file. This advances one
representative workflow, not the full three-case milestone or the broader
production, human-review and quality scopes. Reproduction follows the
[publication guide](../approved-publication.md) with fresh work/report paths.
