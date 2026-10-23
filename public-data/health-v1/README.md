# Authored fictional activity and ROI assumptions

These inputs were authored for the local study on 2026-10-23. They describe no
actual person, customer, work, provider usage, price, invoice, salary or savings.
Scenario event times describe fictional October windows; execution dates are
recorded separately in the actual process report.

`events.json` contains seven usage events: six for Alpha (three previous request
IDs, two current IDs) and one for Beta. Beta deliberately collides with Alpha's
`current-input` event ID but has a distinct request marker/quantity. A separate
late Alpha event exercises a newer report without modifying the earlier export.
The existing fictional-v1 rate card defines exact charges.

`scenario.json` declares adjacent equal-duration UTC windows and bounded ordered
assumptions. Its three independent low/base/high axes generate 27 combinations;
there is no fitted parameter, held-out benchmark or statistical interval. Fixed
cost is already allocated to the selected window by assumption; no amortization,
FX, tax, benefit measurement or successful-task inference is implemented.

All inputs are original authored fixture data distributed under this repository's
MIT license. Source/fixture hashes and true observation times accompany the
[recorded local run](../../reports/health-v1).
