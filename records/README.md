# Research records

The audit trail of decisions taken during development. `out/` is scratch and is
gitignored; these are the outputs that a decision was actually based on, kept so
the reasoning can be re-read later.

| File | What it records |
|---|---|
| `holdout_result.txt` | **The holdout, spent once.** `all_three` went from +0.169R in-fold to −0.132R out of sample; `risk_1p6` held at +0.127R. This is why `config/policy.yaml` is what it is. |
| `walkforward_narrow_universe.txt` | First four-fold run, 85 names — the six original variants |
| `walkforward_defensive.txt` | Cash yield on; the regime gate and bundled-change variants |
| `walkforward_risk_scaling.txt` | The clean single-change risk-envelope test |
| `walkforward_wide_universe.txt` | 503 names — the run that showed the universe was the binding constraint |

None of these can be reproduced by re-running, because the holdout can only be
spent once and every fold has now informed a decision. That is the point of
keeping them.
