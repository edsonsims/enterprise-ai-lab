# Verify a core wheel before an offline installation

This study packages the dependency-free lexical evaluator as a trusted local
wheel, a standalone standard-library verifier and a canonical inventory. It does
not package the optional model, OAuth, OCR, MCP or Temporal runtimes. The inventory
explicitly lists zero separately bundled dependencies, model assets and container
images. Container deployment and the complete roadmap12 pipeline remain planned.

The operator retains the manifest SHA-256 through a separate trusted channel.
Verification requires that exact digest, checks every declared file's bytes and
size, and refuses missing, extra, linked or unexpected paths. A digest protects
comparison with those retained bytes; it is not a publisher signature. The
verifier must start from trusted source. Executing an untrusted copied verifier
and trusting its own answer would not establish trust.

## Prepare on the connected build host

Use Python3.10+ and an already installed setuptools build backend. For the
recorded run the backend was [setuptools84.2.1](https://pypi.org/project/setuptools/84.2.1/),
installed before the experiment.
The build performs no dependency resolution or download:

```sh
python -m pip wheel --no-index --no-deps --no-build-isolation . --wheel-dir .local/core-wheel
PYTHONPATH=src python -m enterprise_ai_lab.offline_bundle create \
  --wheel .local/core-wheel/enterprise_ai_lab-0.12.0-py3-none-any.whl \
  --out-dir .local/core-bundle
```

Retain the returned `manifest_sha256`. A new output directory is required.
Creating two bundles from the same frozen wheel and verifier yields identical
manifest bytes. This is content repeatability with fixed inputs, not a claim that
independent wheel builds have identical ZIP timestamps or bytes.

## Verify and install on a trusted destination

Copy the bundle and bring the expected digest separately. From a trusted checkout
or an already trusted installation of this verifier:

```sh
PYTHONPATH=src python -m enterprise_ai_lab.offline_bundle verify \
  --bundle /path/to/core-bundle --expected-manifest-sha256 EXPECTED_DIGEST
```

The result includes `wheel_relative_path`. Immediately after verification, create
a fresh virtual environment and install that exact path with:

```sh
python -m venv /path/to/fresh-runtime
/path/to/fresh-runtime/bin/python -I -m pip --isolated install \
  --no-index --no-deps --no-cache-dir --disable-pip-version-check \
  /path/to/core-bundle/artifacts/WHEEL_SHA/enterprise_ai_lab-0.12.0-py3-none-any.whl
```

The Python interpreter, standard library, `venv` and bundled `ensurepip` are
prerequisites on the destination. They are not delivered by this wheel inventory.
The wheel contains the lab's optional modules, but their external dependencies
are absent and those integrations are not exercised by this study.

## Observe process network denial on macOS

The actual experiment runs only on macOS with `/usr/bin/sandbox-exec`. It first
delivers TCP and UDP positive controls to parent-owned numeric loopback listeners.
It then applies `(version 1) (allow default) (deny network*)` to a child process.
The child must observe permission denial for TCP and UDP attempts; a descendant
must observe permission denial too. A timeout or refused connection does not
qualify. The parent checks that no attempted sandbox packet/connection arrived.

Within that same network-denied process, the script verifies the copied
standalone verifier, creates a fresh virtual environment, verifies the bundle
again, installs the wheel with no index/dependencies/cache, checks the actual
installed import location and runs the original lexical demo outside the source
checkout. The report records all demo file hashes and preserves the intentionally
unsafe global baseline alongside the permission-filtered result.

```sh
python scripts/offline_bundle_demo.py \
  --wheel .local/core-wheel/enterprise_ai_lab-0.12.0-py3-none-any.whl \
  --work-dir .local/offline-install-demo \
  --out reports/local/offline-install-demo.json
```

Use new work and report paths; existing paths are refused. The network policy
applies to the process and descendants. This test shares the host filesystem,
interpreter and operating system. It is not a container, VM, physical air gap,
hostile-wheel sandbox, production deployment or proof of every possible network
channel. Linux/macOS bundle validation tests are separate from this macOS-only
process experiment. `sandbox-exec` is a platform-specific development facility;
unavailable platforms fail the experiment rather than silently claiming denial.

## Boundaries and exercises

The wheel and build host are trusted. Bounded ZIP metadata inspection is not a
general package safety audit. POSIX owned regular single-link files and physical
directories are required. Source inputs and verification are bounded, but a
hostile host can replace paths after verification; there is no atomic transaction
between verification and pip, administrator-proof authenticity or secure erasure.

Change one copied artifact byte while keeping the independently retained digest;
verification must refuse it. Add an undeclared file, replace a file with a link,
or supply a different manifest digest and inspect the finite error code. Compare
the two same-input inventories. Explain which runtime assets are still supplied
by the host and why this evidence cannot establish a complete air-gapped AI
deployment. No owner study or human review is recorded by running the script.
