# Contributing

Small, focused fixes and experiments are welcome. Open an issue before proposing a
new integration or changing the public data format. Search existing issues and pull
requests first.

Use Python 3.10 or later. Install with `python -m pip install -e .` and run
`python -m unittest discover -s tests -v`. A change to retrieval or evaluation should
include a counterexample or regression test and explain which metric or invariant
it affects. Reproduce the demo after changing experiment behavior.

AI-assisted work is welcome when disclosed. The contributor must understand the
change and verify the stated tests. Never submit generated claims or benchmark
numbers that were not measured. Keep fixture data synthetic and licensed for reuse.

Please avoid submitting security reports containing real personal information.
This is a local educational lab with intentionally unsafe comparison code; it is
not an authentication service and must not be exposed as one.
