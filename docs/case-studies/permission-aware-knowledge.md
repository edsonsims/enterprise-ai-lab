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
Fresh reproduction follows the [publication guide](../approved-publication.md)
with new work/report paths; it has not been performed by this inventory audit.
