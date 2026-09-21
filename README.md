# 30-Day Autonomous Demo Trading Experiment

**2026-09-21 → 2026-10-21.** A MetaTrader 5 demo account (MetaQuotes-Demo,
no real money) trades signals from the NovaSignals EA
(EMA20/EMA50 cross + RSI, M15, 77 symbols) through a risk-gated autonomous
executor.

Everything is logged: `evidence/` holds weekly journal slices, review
proposals, reflections, and the day-30 `FINAL_REPORT.md` — wins **and**
losses, exactly as they happened.

- [EXPERIMENT_CHARTER.md](EXPERIMENT_CHARTER.md) — the rules (risk bounds,
  market hours, kill-switch discipline, learning loop)
- `trader/` — the executor, market-hours gate, NovaTrader EA source,
  review/reflection/report tooling, and the full test suite
- `evidence/` — the audit trail

The goal was for wins to outweigh losses. The final report shows what
actually happened — no cherry-picking, no fabrication.
