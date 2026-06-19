"""
가상 매매 시뮬레이터.
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, field, asdict
from typing import Optional, List
import config

def calc_costs_pct(market):
    cfg = config.RULE_V2
    fee = cfg["fee_buy_pct"] + cfg["fee_sell_pct"]
    tax = cfg["tax_kosdaq_pct"] if market.upper() == "KOSDAQ" else cfg["tax_kospi_pct"]
    slip = cfg["slippage_pct"] * 2
    return fee + tax + slip

@dataclass
class Trade:
    mode: str
    stock_code: str
    stock_name: str
    market: str
    entry_time: pd.Timestamp
    entry_price: float
    exit_time: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    gross_pct: float = 0.0
    cost_pct: float = 0.0
    net_pct: float = 0.0
    high_after_entry: float = 0.0
    holding_minutes: int = 0
    partial_tp_price: Optional[float] = None
    partial_tp_pct: Optional[float] = None

    def finalize(self):
        if self.exit_price is None or self.entry_price is None:
            return
        self.gross_pct = (self.exit_price / self.entry_price - 1) * 100
        self.cost_pct = calc_costs_pct(self.market)

        if self.partial_tp_pct is not None:
            half_a = self.partial_tp_pct
            half_b = self.gross_pct
            self.gross_pct = (half_a + half_b) / 2

        self.net_pct = self.gross_pct - self.cost_pct
        if self.exit_time is not None and self.entry_time is not None:
            self.holding_minutes = int(
                (self.exit_time - self.entry_time).total_seconds() // 60
            )

class Simulator:
    def __init__(self, mode, stock_code, stock_name, market):
        assert mode in ("A", "B", "AB"), "mode must be A, B, or AB"
        self.mode = mode
        self.stock_code = stock_code
        self.stock_name = stock_name
        self.market = market
        self.cfg = config.RULE_V2

    def simulate(self, df):
        trades = []
        in_position = False
        entry_idx = None
        entry_price = None
        entry_time = None
        target_price = None
        stop_price = None
        high_after_entry = None
        partial_tp_done = False
        partial_tp_price = None
        partial_tp_pct = None
        mode_locked = None
        exit_deadline_idx = None

        rows = df.reset_index(drop=True)

        for i in range(len(rows)):
            row = rows.iloc[i]

            if in_position:
                high_after_entry = max(high_after_entry, row["high"])

                # v2.7 조기 종료 — 진입 후 early_exit_minutes 시점에 평가.
                # 손실 임계값 이하면 시간청산까지 안 기다리고 즉시 종료.
                # 1차 익절 이미 했으면 (partial_tp_done) skip — 트레일링이 보호.
                if (self.cfg.get("early_exit_enabled", False)
                        and not partial_tp_done
                        and i - entry_idx == self.cfg.get("early_exit_minutes", 10)):
                    cur_pct = (row["close"] / entry_price - 1) * 100
                    if cur_pct <= self.cfg.get("early_exit_threshold_pct", -0.5):
                        t = self._close_trade(
                            i_entry=entry_idx, entry_price=entry_price,
                            entry_time=entry_time, exit_idx=i, exit_row=row,
                            exit_price=row["close"], reason="early_exit",
                            partial_tp_price=partial_tp_price, partial_tp_pct=partial_tp_pct,
                            high_after_entry=high_after_entry,
                        )
                        trades.append(t)
                        in_position = False
                        continue

                if i >= exit_deadline_idx:
                    t = self._close_trade(
                        i_entry=entry_idx, entry_price=entry_price,
                        entry_time=entry_time, exit_idx=i, exit_row=row,
                        exit_price=row["open"], reason="time",
                        partial_tp_price=partial_tp_price, partial_tp_pct=partial_tp_pct,
                        high_after_entry=high_after_entry,
                    )
                    trades.append(t)
                    in_position = False
                    continue

                if row["low"] <= stop_price:
                    t = self._close_trade(
                        i_entry=entry_idx, entry_price=entry_price,
                        entry_time=entry_time, exit_idx=i, exit_row=row,
                        exit_price=stop_price, reason="stop_loss",
                        partial_tp_price=partial_tp_price, partial_tp_pct=partial_tp_pct,
                        high_after_entry=high_after_entry,
                    )
                    trades.append(t)
                    in_position = False
                    continue

                if mode_locked == "A":
                    if row["high"] >= target_price:
                        t = self._close_trade(
                            i_entry=entry_idx, entry_price=entry_price,
                            entry_time=entry_time, exit_idx=i, exit_row=row,
                            exit_price=target_price, reason="box_target",
                            partial_tp_price=partial_tp_price, partial_tp_pct=partial_tp_pct,
                            high_after_entry=high_after_entry,
                        )
                        trades.append(t)
                        in_position = False
                        continue

                    if self.mode == "AB" and row.get("is_volume_surge"):
                        mode_locked = "B"

                if mode_locked == "B":
                    tp1_price = entry_price * (1 + self.cfg["tp_first_pct"] / 100)
                    just_hit_tp1 = False  # [수정된 부분] 이번 캔들에서 익절되었는지 체크하는 플래그
                    
                    if not partial_tp_done and row["high"] >= tp1_price:
                        partial_tp_done = True
                        partial_tp_price = tp1_price
                        partial_tp_pct = self.cfg["tp_first_pct"]
                        just_hit_tp1 = True
                        high_after_entry = max(high_after_entry, row["high"])

                    # [수정된 부분] 방금 익절된 동일 캔들 안에서는 트레일링 스탑을 발동시키지 않음
                    if partial_tp_done and not just_hit_tp1:
                        trailing_stop = high_after_entry * (
                            1 - self.cfg["trailing_drawdown_pct"] / 100
                        )
                        if row["low"] <= trailing_stop:
                            t = self._close_trade(
                                i_entry=entry_idx, entry_price=entry_price,
                                entry_time=entry_time, exit_idx=i, exit_row=row,
                                exit_price=trailing_stop,
                                reason="tp_partial+trailing",
                                partial_tp_price=partial_tp_price,
                                partial_tp_pct=partial_tp_pct,
                                high_after_entry=high_after_entry,
                            )
                            trades.append(t)
                            in_position = False
                            continue

                continue

            # ============ 미보유 → 진입 판단 ============
            if not row.get("is_box"):
                continue

            box_high = row["box_high"]
            box_low = row["box_low"]
            box_width = box_high - box_low
            offset = box_width * self.cfg["entry_offset_pct_of_box"] / 100

            entry_a = box_low + offset
            target_a = box_high - offset

            entered = False
            entered_mode = None
            entered_price = None

            if self.mode in ("A", "AB"):
                if row["low"] <= entry_a <= row["high"]:
                    entered_price = entry_a
                    entered_mode = "A"
                    entered = True

            if not entered and self.mode in ("B", "AB"):
                if row.get("is_volume_surge"):
                    # [수정된 부분] 진짜 돌파만 잡기: 종가가 박스 상단을 명확히 위 (close > box_high).
                    # 박스 안 매집봉/윗꼬리/가짜 돌파는 제외.
                    if row["close"] > box_high:
                        entered_price = row["close"]
                        entered_mode = "B"
                        entered = True

            if not entered:
                continue

            in_position = True
            entry_idx = i
            entry_price = entered_price
            entry_time = row["datetime"]
            high_after_entry = row["high"]
            partial_tp_done = False
            partial_tp_price = None
            partial_tp_pct = None
            mode_locked = entered_mode
            target_price = target_a if entered_mode == "A" else None
            stop_price = entry_price * (1 + self.cfg["stop_loss_pct"] / 100)
            exit_deadline_idx = i + self.cfg["time_exit_minutes"]

        if in_position:
            last = rows.iloc[-1]
            t = self._close_trade(
                i_entry=entry_idx, entry_price=entry_price,
                entry_time=entry_time, exit_idx=len(rows) - 1, exit_row=last,
                exit_price=last["close"], reason="market_close",
                partial_tp_price=partial_tp_price, partial_tp_pct=partial_tp_pct,
                high_after_entry=high_after_entry,
            )
            trades.append(t)

        return trades

    def _close_trade(self, i_entry, entry_price, entry_time, exit_idx,
                     exit_row, exit_price, reason,
                     partial_tp_price, partial_tp_pct, high_after_entry):
        t = Trade(
            mode=self.mode,
            stock_code=self.stock_code,
            stock_name=self.stock_name,
            market=self.market,
            entry_time=entry_time,
            entry_price=entry_price,
            exit_time=exit_row["datetime"],
            exit_price=exit_price,
            exit_reason=reason,
            high_after_entry=high_after_entry,
            partial_tp_price=partial_tp_price,
            partial_tp_pct=partial_tp_pct,
        )
        t.finalize()
        return t

def run_simulation(date_str, mode="AB"):
    import data_loader
    import detector

    bars_by_code = data_loader.load_all_for_date(date_str)
    name_map = data_loader.get_stock_name_map(date_str)
    market_default = "KOSDAQ"

    all_trades = []
    for code, df in bars_by_code.items():
        if len(df) < 20:
            continue
        annotated = detector.annotate_signals(df)
        sim = Simulator(
            mode=mode,
            stock_code=code,
            stock_name=name_map.get(code, code),
            market=market_default,
        )
        trades = sim.simulate(annotated)
        all_trades.extend(trades)
    return all_trades

if __name__ == "__main__":
    import sys
    import data_loader

    dates = data_loader.list_available_dates()
    if not dates:
        print("수집된 데이터가 없습니다. 먼저 data_collector.py 실행하세요.")
        sys.exit(1)

    date_str = sys.argv[1] if len(sys.argv) > 1 else dates[-1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "AB"

    print(f"[{date_str}] 모드 {mode} 시뮬레이션 실행 중...")
    trades = run_simulation(date_str, mode=mode)
    print(f"\n매매 건수: {len(trades)}")
    if trades:
        for t in trades[:20]:
            print(
                f"  {t.stock_name:12s} {t.entry_time.strftime('%H:%M')} → "
                f"{t.exit_time.strftime('%H:%M')} "
                f"net={t.net_pct:+.2f}% ({t.exit_reason})"
            )