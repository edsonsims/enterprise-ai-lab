# Historical case inventory: first byte-audit observation

This records a new execution of the dependency-free auditor on2026-10-28, not new
publication, failover or model experiments. All three fixed archived reports match
their original byte lengths and SHA-256 values. Their dates, failures, working-tree
fingerprints and source limits remain unchanged.

[`audit.json`](audit.json) contains only pinned metadata, observed hashes/sizes and
finite evidence states. [`source.json`](source.json) records the new auditor's
working-tree fingerprints, installed0.14.1 wheel hash and actual runtime. The wheel
was installed without dependencies into a fresh environment and invoked with
Python isolated mode outside the source checkout, using an explicit repository
path. No registry release occurred.

The21 new contract tests and14 original lexical tests pass. Repeating these cases
on multiple interpreters does not create new unique cases. An additional system
Python3.9 run passed but does not extend the declared Python3.10+ support.

```sh
python -m enterprise_ai_lab.case_studies inspect --repository .
python -W error::ResourceWarning -m unittest discover -s tests/case_studies -v
```

A new inspection has a new timestamp; matching the archived report bytes does not
reproduce or semantically verify them. Fresh end-to-end reproductions, the complete
three-case milestone and larger planned study scopes remain pending. No owner
review, personal study, new inference, model-quality or production claim follows.
