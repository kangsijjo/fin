"""
백테스트 메인 실행 스크립트.

사용법:
  # 수집된 모든 날짜를 한 번에 백테스트 (모드 A, B, AB 모두 비교)
  python backtest.py

  # 특정 날짜만
  python backtest.py 20260515

  # 특정 모드만
  python backtest.py 20260515 AB

결과:
  - 콘솔에 요약 리포트 출력
  - results/trades_MODE_YYYYMMDD-YYYYMMDD.csv 에 매매 내역 저장
  - results/daily_MODE_YYYYMMDD-YYYYMMDD.csv 에 일별 손익 저장
"""

import sys
import os
from datetime import datetime

import config
import data_loader
import detector
from simulator import Simulator
from data_collector import _is_excluded
import analyzer


MODES = ["A", "B", "AB"]


def run_one_date(date_str, mode):
    """
    한 날짜, 한 모드 시뮬레이션.

    Returns: (trades, info)
      trades: Trade 리스트
      info: {'filter_applied': bool, 'd1_date': str|None,
             'skipped_no_d1_data': int, 'skipped_negative': int,
             'eligible_codes': int}
    """
    bars_by_code = data_loader.load_all_for_date(date_str)
    name_map = data_loader.get_stock_name_map(date_str)
    info = {
        'filter_applied': bool(config.ENABLE_FOREIGN_FILTER),
        'd1_date': None,
        'skipped_no_d1_data': 0,
        'skipped_negative': 0,
        'skipped_etf': 0,
        'eligible_codes': 0,
    }
    if not bars_by_code:
        return [], info

    # D-1 외인 순매수 필터 데이터 로드
    foreign_filter_data = None
    if config.ENABLE_FOREIGN_FILTER:
        prev_date = data_loader.previous_trading_day(date_str)
        info['d1_date'] = prev_date
        if prev_date:
            foreign_filter_data = data_loader.load_investor_for_date(prev_date)

    trades = []
    for code, df in bars_by_code.items():
        if len(df) < 20:
            continue

        # 분석 필터: ETF / 우선주 / 관리종목 제외 (수집은 raw, 시뮬은 일반주만)
        if config.ENABLE_ETF_FILTER:
            name = name_map.get(code, "")
            if _is_excluded(name, code):
                info['skipped_etf'] += 1
                continue

        # 진입 필터: D-1 외인 순매수 > 0 인 종목만
        if config.ENABLE_FOREIGN_FILTER:
            if not foreign_filter_data:
                info['skipped_no_d1_data'] += 1
                continue
            d1_row = foreign_filter_data.get(code)
            if d1_row is None:
                info['skipped_no_d1_data'] += 1
                continue
            if float(d1_row.get('foreign_net_value', 0) or 0) <= 0:
                info['skipped_negative'] += 1
                continue

        info['eligible_codes'] += 1
        annotated = detector.annotate_signals(df)
        sim = Simulator(
            mode=mode,
            stock_code=code,
            stock_name=name_map.get(code, code),
            market="KOSDAQ",
        )
        trades.extend(sim.simulate(annotated))
    return trades, info


def main():
    args = sys.argv[1:]

    date_filter = None
    mode_filter = None
    for a in args:
        if a.isdigit() and len(a) == 8:
            date_filter = a
        elif a.upper() in MODES:
            mode_filter = a.upper()

    all_dates = data_loader.list_available_dates()
    if not all_dates:
        print("[error] 수집된 분봉 데이터가 없습니다.")
        print("        먼저 `python data_collector.py today` 실행 후 데이터를 누적하세요.")
        sys.exit(1)

    dates = [date_filter] if date_filter else all_dates
    modes = [mode_filter] if mode_filter else MODES

    print(f"백테스트 대상: {len(dates)}일 × {len(modes)}모드")
    print(f"기간: {dates[0]} ~ {dates[-1]}")
    print(f"룰 v2: 박스 {config.RULE_V2['box_window_minutes']}분/"
          f"±{config.RULE_V2['box_max_pct']}%, "
          f"거래량 {config.RULE_V2['volume_surge_multiplier']}x, "
          f"1차익절 +{config.RULE_V2['tp_first_pct']}%, "
          f"트레일링 -{config.RULE_V2['trailing_drawdown_pct']}%, "
          f"시간청산 {config.RULE_V2['time_exit_minutes']}분, "
          f"손절 {config.RULE_V2['stop_loss_pct']}%")
    filter_state = "ON (D-1 외인 순매수 > 0)" if config.ENABLE_FOREIGN_FILTER else "OFF"
    print(f"진입 필터: {filter_state}")
    etf_state = "ON (_is_excluded)" if config.ENABLE_ETF_FILTER else "OFF"
    print(f"ETF 분석 필터: {etf_state}")
    print()

    summaries = []
    exit_reasons_per_mode = {}
    period = f"{dates[0]}-{dates[-1]}"

    for mode in modes:
        print(f"[모드 {mode}] 시뮬레이션...")
        all_trades = []
        for d in dates:
            trades, info = run_one_date(d, mode)
            all_trades.extend(trades)
            parts = []
            if config.ENABLE_ETF_FILTER and info['skipped_etf'] > 0:
                parts.append(f"skip_etf={info['skipped_etf']}")
            if info['filter_applied']:
                parts.append(f"D-1={info['d1_date'] or 'NONE'}, "
                             f"eligible={info['eligible_codes']}, "
                             f"skip_neg={info['skipped_negative']}, "
                             f"skip_nodata={info['skipped_no_d1_data']}")
            suffix = f" [{', '.join(parts)}]" if parts else ""
            print(f"  {d}: {len(trades)}건{suffix}")

        summary = analyzer.summarize(all_trades, label=f"mode_{mode}")
        summaries.append(summary)
        exit_reasons_per_mode[mode] = analyzer.exit_reason_breakdown(all_trades)

        trades_path = f"{config.RESULT_DIR}/trades_{mode}_{period}.csv"
        analyzer.save_trades_csv(all_trades, trades_path)

        daily = analyzer.daily_summary(all_trades)
        daily_path = f"{config.RESULT_DIR}/daily_{mode}_{period}.csv"
        if not daily.empty:
            daily.to_csv(daily_path, index=False, encoding="utf-8-sig")

        print(f"  → 매매 {len(all_trades)}건 저장: {trades_path}")
        print()

    analyzer.print_report(summaries, exit_reasons_per_mode)

    print("\n" + "=" * 78)
    print(" 해석 가이드")
    print("=" * 78)
    print("""
 - n_trades       : 백테스트 기간 동안 발생한 매매 건수
 - win_rate_pct   : 승률 (수익 매매 비율)
 - avg_net_pct    : 매매당 평균 순수익률 (비용 제외)
 - avg_win_pct    : 익절 시 평균 수익률
 - avg_loss_pct   : 손절 시 평균 손실률
 - profit_factor  : 손익비 (총이익 / 총손실 절댓값). 1.5 이상이면 양호
 - cum_pct        : 매매당 net_pct의 단순 합 (자본 단위 아님; 매매 효율 지표)
 - mdd_pct        : 최대 연속 손실 구간 (매매 순서 기준)
 - avg_holding_min: 평균 보유 시간(분)

 결과 파일:
   results/trades_*.csv  - 매매 한 건 한 건 상세
   results/daily_*.csv   - 일별 손익 요약
""")


if __name__ == "__main__":
    main()
