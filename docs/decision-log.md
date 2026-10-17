# Decision log

## 2026-10-17: one connected lab, evaluation first

**Context.** The goal is a balanced portfolio for senior software engineering and
applied AI/research engineering. The source list contains interdependent enterprise
components, many of which require identity systems, special hardware, real data, or
deployment environments.

**Decision.** Start with a dependency-light offline evaluation harness and a
permission-aware lexical retrieval fixture. Grow a connected lab rather than
declaring 15 complete projects. Keep core interfaces small enough to extract useful
standalone tools later.

**Reasoning.** The first experiment can run without credentials, customer data,
cloud spend, or special hardware. It establishes data contracts and negative tests
that later integrations must pass. The initial retrieval mechanism is deliberately
simple so permission and measurement behavior can be inspected directly.

**Alternatives.** Starting with a full identity deployment gives a stronger live
integration but delays measurement. Starting with 15 repositories makes scope easy
to browse but creates maintenance overhead before the components exist.

**Limits.** Lexical retrieval and fixture identities do not establish LLM answer
quality or production authorization. The synthetic cases are designed tests, not
an independent sample of customer traffic. AI-assisted implementation and automated
review are disclosed separately from the owner's personal study.
