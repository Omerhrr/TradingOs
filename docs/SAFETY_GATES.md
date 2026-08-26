# TradingOS Safety Gates

TradingOS is engineered as a **practice-first system**, not as an unchecked route to a funded account. The database begins with `PRACTICE`, the real-mode endpoint returns `403`, and runtime health must report `live_execution: false`. The automated `scripts/verify_practice_boundary.py` check is part of every local startup and VDS migration acceptance test.

| Gate | Current behavior | What would be required to move forward |
|---|---|---|
| Broker identity | Credentials are encrypted at rest and connection selects `PRACTICE`, then verifies the resulting balance mode. | Account connection and reconciliation must succeed repeatedly without mode drift. |
| Market freshness | An intent is rejected when its latest candle is older than the policy threshold. | Freshness behaviour must be proven under disconnect and restart conditions. |
| Risk approval | Exposure, trade amount, daily-loss, drawdown, broker state, system state, and strategy status are checked deterministically. | Results must be reviewed across meaningful practice history. |
| Strategy quality | A strategy starts as `DRAFT`; only time-ordered, censored validation can mark it `VALIDATED`. | Separate out-of-sample and long practice observation periods are required. |
| AI output | AI is opt-in, rate-limited, token-budgeted, and returns structured hypotheses only. It has no broker or order import path. | Any change to that separation requires a separate security review. |
| Practice execution | Intent creation is separate from submission. Submission defaults to disabled. | The operator must explicitly change the local practice setting after verifying all prior gates. |
| Real execution | There is no real-order implementation and real mode is hard-disabled. | This codebase deliberately provides no activation procedure for a funded account. |

> A passed backtest or an accepted strategy evaluation is **not** evidence that future trades will be profitable. Practice-order submission remains the maximum capability designed into this version.
