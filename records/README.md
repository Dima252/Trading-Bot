# Research records

The audit trail of decisions taken during development. `out/` is scratch and is
gitignored; these are the outputs that a decision was actually based on, kept so
the reasoning can be re-read later.

| File | What it records |
|---|---|
| `gonogo_screened.txt` | **The corrected go/no-go.** Re-run once the backtest was made to apply the live liquidity screen. 4/4 folds positive, mean +0.157R. Supersedes the run below. |
| `gonogo_screened_sigma.txt` | Sigma on the screened trades (1.4631, t = 2.99) plus the per-setup breakdown that showed pullback carries the whole system. |
| `gonogo_1993_2013.txt` | **The go/no-go, 2026-08-29.** The shipped config over 20 years it had never seen, folded across the dot-com crash and the GFC. Criteria were pre-registered in PLAN §5f before the data was fetched. 3/3, +0.118R against baseline's +0.007R over 847 trades. |
| `sleeve_b_1993_2013.txt` | **Sleeve B, failed.** H1 was byte-identical to the shipped config -- mean_reversion produces 0.47% of candidates and never wins a slot. Combined Sharpe 0.285 against sleeve A's 0.372. Criteria in PLAN §5g, result in §5g-R. |
| `gonogo_sigma_measured.txt` | The measured per-trade R dispersion, 1.3954 over 845 trades. The go/no-go's significance turns on it, so it was measured rather than assumed. |
| `sleeve_c_correlation.txt` | **Sleeve C, failed 2 of 4.** Multi-asset trend correlated +0.435 to sleeve A against a 0.30 bar, because 61% of its book sat in equities. Criteria in PLAN §5i, result in §5i-R. |
| `holdout_result.txt` | **The holdout, spent once.** `all_three` went from +0.169R in-fold to −0.132R out of sample; `risk_1p6` held at +0.127R. This is why `config/policy.yaml` is what it is. |
| `walkforward_narrow_universe.txt` | First four-fold run, 85 names — the six original variants |
| `walkforward_defensive.txt` | Cash yield on; the regime gate and bundled-change variants |
| `walkforward_risk_scaling.txt` | The clean single-change risk-envelope test |
| `walkforward_wide_universe.txt` | 503 names — the run that showed the universe was the binding constraint |

None of these can be reproduced by re-running, because the holdout can only be
spent once and every fold has now informed a decision. That is the point of
keeping them.

The 1993-2013 window is the exception and is deliberately kept explorable: it is
the development set, and `walkforward` now refuses contaminated ranges outright
(`trading_bot/provenance.py`). What is spent is 2014-2019, the validation
window, which has not been touched.
