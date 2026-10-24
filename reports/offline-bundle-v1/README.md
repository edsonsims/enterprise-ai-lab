# Core wheel under process network denial

This frozen2026-10-24 macOS arm64 observation uses Python3.13.1 and a local
0.12.0 wheel built with setuptools84.2.1/pip24.3.1. The report retains the exact
wheel and standalone verifier hashes. The wheel was not published to a registry.
Source fingerprints describe the tested working tree; the base commit predates
this implementation. Later changes must not rewrite this report.

Two bundles from the same frozen wheel/verifier produced the same manifest
digest. TCP and UDP controls delivered to owned listeners before isolation.
Under the macOS process policy, TCP, UDP and a descendant observed `EPERM`;
the parent observed no sandbox delivery. Within that process a fresh venv
installed the wheel without index/dependencies/cache, imported from its own
site-packages, and executed the original lexical demo outside the checkout.

The installed environment contained only pip and enterprise-ai-lab. The original
synthetic safe query-leakage value was0; the deliberately unsafe global baseline
was0.9. All12 demo artifact hashes are recorded. This repeats existing fictional
retrieval behavior, not new model-quality or production-security evidence.

Twenty focused ZIP/filesystem/standalone-verifier tests passed on Python3.10.16
and3.13.1; these are the same20 methods, not40 distinct cases. The original14
tests passed separately. Independent validation repeated the actual sandbox
experiment and checked additional installed-wheel/runner observations. Automated
execution is not owner study or human review.

The host supplies the interpreter, ensurepip and filesystem. Dependency/model/
container image inventories are explicitly empty. No filesystem isolation,
physical air gap, hostile-wheel safety, optional integration, container delivery,
cloud infrastructure or production deployment is established.

See [the contract and reproduction commands](../../docs/offline-bundle.md),
[the raw observation](demo.json) and [source fingerprints](source.json).
