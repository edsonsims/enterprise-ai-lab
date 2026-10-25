# CPU inference requests v1

These three question/context pairs were authored for this repository. They describe
fictional batch, maintenance and archive policies. They contain no customer material,
public-dataset labels or expected answers. They are available under the repository's
MIT license; the model has its own Apache-2.0 license.

The UTF-8 bytes of `requests-v1.json` are frozen at SHA-256
`f1e31ca721496b9c3d4ac0b8feff33d450807309e61ecef200935a76caaad57c`.
The runtime refuses a changed fixture. The protocol fixes one first request after
model load followed by two passes over all three cases, for each of the sequential
1- and 4-intraop-thread workers: 7 requests per configuration, 14 planned calls.
Every first-request result and every generation-format failure remains in the report.

This fixture measures execution behavior only. Exact copied evidence is not proof
that an answer is relevant, complete or true. There is no quality score, held-out
split, customer validation or comparison between different models.
