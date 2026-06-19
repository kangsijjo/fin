@echo off
cd /d C:\fin\outputs
echo === Deleting legacy files ===

if exist adjusted_probe.py       del adjusted_probe.py       & echo DELETED: adjusted_probe.py
if exist ai_feature_engine.py    del ai_feature_engine.py    & echo DELETED: ai_feature_engine.py
if exist ai_trainer.py           del ai_trainer.py           & echo DELETED: ai_trainer.py
if exist ai_trainer_v2.py        del ai_trainer_v2.py        & echo DELETED: ai_trainer_v2.py
if exist ai_trainer_v3.py        del ai_trainer_v3.py        & echo DELETED: ai_trainer_v3.py
if exist backfill_history.py     del backfill_history.py     & echo DELETED: backfill_history.py
if exist backtest_swing.py       del backtest_swing.py       & echo DELETED: backtest_swing.py
if exist compare_exit_rules.py   del compare_exit_rules.py   & echo DELETED: compare_exit_rules.py
if exist compare_trailing_fine.py del compare_trailing_fine.py & echo DELETED: compare_trailing_fine.py
if exist compare_trailing_grid.py del compare_trailing_grid.py & echo DELETED: compare_trailing_grid.py
if exist dart_fundamentals.py    del dart_fundamentals.py    & echo DELETED: dart_fundamentals.py
if exist dashboard.py            del dashboard.py            & echo DELETED: dashboard.py
if exist debug_signal.py         del debug_signal.py         & echo DELETED: debug_signal.py
if exist diag_env.py             del diag_env.py             & echo DELETED: diag_env.py
if exist ensemble_check.py       del ensemble_check.py       & echo DELETED: ensemble_check.py
if exist fdr_probe.py            del fdr_probe.py            & echo DELETED: fdr_probe.py
if exist fix_paper_signals_names.py del fix_paper_signals_names.py & echo DELETED: fix_paper_signals_names.py
if exist index_diag.py           del index_diag.py           & echo DELETED: index_diag.py
if exist index_probe.py          del index_probe.py          & echo DELETED: index_probe.py
if exist index_probe2.py         del index_probe2.py         & echo DELETED: index_probe2.py
if exist index_probe3.py         del index_probe3.py         & echo DELETED: index_probe3.py
if exist index_probe4.py         del index_probe4.py         & echo DELETED: index_probe4.py
if exist index_probe5.py         del index_probe5.py         & echo DELETED: index_probe5.py
if exist index_probe6.py         del index_probe6.py         & echo DELETED: index_probe6.py
if exist kis_websocket.py        del kis_websocket.py        & echo DELETED: kis_websocket.py
if exist kiwoom_backfill.py      del kiwoom_backfill.py      & echo DELETED: kiwoom_backfill.py
if exist make_trades_history.py  del make_trades_history.py  & echo DELETED: make_trades_history.py
if exist monthly_xlsx_builder.py del monthly_xlsx_builder.py & echo DELETED: monthly_xlsx_builder.py
if exist nxt_probe.py            del nxt_probe.py            & echo DELETED: nxt_probe.py
if exist pykrx_backtester.py     del pykrx_backtester.py     & echo DELETED: pykrx_backtester.py
if exist seed_paper_signals.py   del seed_paper_signals.py   & echo DELETED: seed_paper_signals.py
if exist seed_paper_simple.py    del seed_paper_simple.py    & echo DELETED: seed_paper_simple.py
REM strategy_engine.py 는 현재 사용 중 — 삭제 제외 (2026-06-18)
if exist strategy_report.py      del strategy_report.py      & echo DELETED: strategy_report.py
if exist survivorship_check.py   del survivorship_check.py   & echo DELETED: survivorship_check.py
if exist us_market_collector.py  del us_market_collector.py  & echo DELETED: us_market_collector.py
if exist walkforward.py          del walkforward.py          & echo DELETED: walkforward.py

echo.
echo === Done ===
pause
