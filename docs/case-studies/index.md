# Local case studies: historical inventory and fresh evidence

These three notes connect existing studies through their trust, reliability and
measurement boundaries. The historical byte
inventory remains unchanged. A separate [fresh permission-aware knowledge
run](../../reports/case-knowledge-v1) was recorded on 2026-10-29 from an installed
wheel with actual identity, native/OCR, approval and vector services. A separate
[fresh reliability case](../../reports/case-reliability-v1), recorded 2026-10-30,
compares actual MCP failover, local approval and scoped redaction as three
separate experiments. A [fresh operability case](../../reports/case-operability-v1),
recorded 2026-10-31, adds actual core installation, CPU inference, fictional
accounting and configuration recovery as four separate experiments. All three
representative cases now have fresh records and separate contract verifiers.
Broader deployment scopes in the individual studies and owner study remain
incomplete; these local cases do not establish production deployments.

- [Permission-aware knowledge](permission-aware-knowledge.md)
- [Reliable legacy automation](reliable-legacy-automation.md)
- [Operable local AI](operable-local-ai.md)

From the source checkout:

```sh
PYTHONPATH=src python -m enterprise_ai_lab.case_studies inspect --repository .
# Optional new output; its parent directory must already exist:
PYTHONPATH=src python -m enterprise_ai_lab.case_studies inspect \
  --repository . --out /path/to/new-audit.json
PYTHONPATH=src python -W error::ResourceWarning -m unittest discover -s tests/case_studies -v
```

The installed wheel supports the same module command with an explicit repository
path. Only the standard library is required. The auditor reads
[`historical-v1.json`](historical-v1.json) and its three fixed report paths. It
does not import optional runtimes, invoke commands, verify report semantics,
start services, download assets or call a model. The manifest itself has a
version-specific byte pin in the auditor; it is not an arbitrary file-export API.

`byte_verified_historical` means that the archived report's length and SHA-256
match this inventory. It does not mean that the recorded experiment was repeated,
that its source revision was checked out, or that a person reviewed it. Archive
revisions identify where the source and report were first published together;
some experiments recorded working-tree fingerprints before that commit existed.
Current source may differ from that historical source.

The output contains fixed case metadata, expected/observed byte hashes and sizes,
finite failure codes, the inspection time and explicit evidence limits. It never
copies report bodies, runtime paths, logs, databases, credentials or tenant text.
Missing or mismatching reports stay visible beside matching cases and make the
command exit 1. Invalid manifests/arguments and refused output creation exit 2.
Symlinks, multiply linked or nonregular artifacts, oversize reads and changes
during a read are refused. Existing outputs are never replaced.
This first implementation uses POSIX directory-handle and no-follow APIs;
Windows portability has not been established.

Fresh reproductions use matching documented runtimes and new private work/report
paths, preserving the old reports and their failures. The first fresh case has a
separate standard-library recorded-contract verifier:

```sh
python scripts/verify_knowledge_case.py --report reports/case-knowledge-v1/demo.json
```

It checks recorded case results and exported history payloads, not new service
execution. Its report explicitly labels a failed-history public derivative that
omits private runtime stack traces; actual SDK replay used the private originals.
Neither verifier can promote archived bytes into a fresh run or complete a larger
study scope.

The fresh reliability case also has a separate recorded-contract verifier:

```sh
python scripts/verify_reliability_case.py --directory reports/case-reliability-v1
```

It checks the three recorded experiments and five exported approval histories.
It performs no execution or SDK replay and does not connect their databases or
promote the local study into a production write/action pipeline.

The operability case has its own standard-library recorded-contract verifier:

```sh
python scripts/verify_operability_case.py --directory reports/case-operability-v1
```

It checks the four reports and their source/artifact bindings without running a
model, starting a process or accessing a live ledger. Inference measurements,
fictional accounting and administration stay separate; the core bundle does not
deliver the optional runtime or weights.
