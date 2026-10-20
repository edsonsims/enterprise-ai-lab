# Fictional privacy development fixture

`synthetic-v1.json` contains 36 authored fictional cases, IDs `dev-001` through
`dev-036`. It includes reserved example domains, fictional people/contact text,
synthetic card-like strings, negative contexts and deliberately unsupported
formats. It contains no collected personal records or external dataset.

The fixture was frozen before its first metric run on 2026-10-20. SHA-256:
`38974426ef1c84ae6899de815164df51e72cbeface00dc4abc7015e36fbf9279`.
The code validates that hash and all IDs/Unicode spans. Gold annotations remain
outside runtime detector inputs. This is a development corpus; neither its
small pattern counts nor unsupported label coverage estimate real-world privacy
protection. The complete protocol, failures and limits are in
[the study notes](../../../../docs/privacy-proxy.md).
